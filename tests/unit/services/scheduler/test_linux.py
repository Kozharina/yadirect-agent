"""Tests for Linux systemd ``--user`` scheduler (M15.6 slice 2).

Mirrors ``test_macos.py``: the surface contract is identical
(``install`` / ``status`` / ``remove`` + atomic writes + a single
indirection point for the platform CLI). The wire format is what
differs — four ``.service`` / ``.timer`` unit files instead of two
``.plist`` files, and ``systemctl --user`` instead of ``launchctl``.

Two layers:

1. Pure unit-file generators — no I/O, no subprocess. We assert
   on the exact systemd directives we care about (``ExecStart``,
   ``OnCalendar``, ``Persistent``, ``OnUnitActiveSec``,
   ``WantedBy``). Round-tripping through a parser would be ideal
   but stdlib has none and pulling ``systemd-python`` for a test
   dep is wrong on macOS dev boxes; pinning the directives by
   substring is good enough — operators see the failures
   immediately if a regression silently rewrites a unit.
2. ``LinuxScheduler.install`` / ``status`` / ``remove`` — replaces
   ``systemctl`` with an in-memory spy via monkeypatch; tempfile
   atomicity verified the same way slice 1 does it (sibling
   tempfile + ``os.replace``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from yadirect_agent.services.scheduler.linux import (
    DAILY_UNIT_NAME,
    HOURLY_UNIT_NAME,
    LinuxInstallResult,
    LinuxScheduler,
    generate_daily_service,
    generate_daily_timer,
    generate_hourly_service,
    generate_hourly_timer,
)


class TestUnitGeneration:
    def test_daily_service_pins_oneshot_and_health_invocation(self) -> None:
        # ``Type=oneshot`` is the systemd idiom for "fire-and-exit"
        # jobs — a long-running ``simple`` service would log
        # spurious "service started/stopped" pairs every cron tick
        # and confuse ``systemctl status``. Health checks are
        # always one-shot.
        executable = "/usr/local/bin/yadirect-agent"
        log_dir = Path("/home/anna/.local/state/yadirect-agent/logs")

        unit = generate_daily_service(executable=executable, log_dir=log_dir)

        assert "[Service]" in unit
        assert "Type=oneshot" in unit
        # ExecStart must use the absolute executable and the same
        # ``health --days=7 --json`` invocation as slice 1's daily
        # plist — the daily run summarises the past week.
        assert f"ExecStart={executable} health --days=7 --json" in unit
        # Logs land in a stable spot the operator can ``tail -f``.
        # ``append:`` (vs ``file:``) keeps the log across runs
        # rather than truncating at every fire.
        assert f"StandardOutput=append:{log_dir}/daily.log" in unit
        assert f"StandardError=append:{log_dir}/daily.err" in unit

    def test_daily_timer_pins_calendar_and_persistent(self) -> None:
        # ``OnCalendar=*-*-* 08:00:00`` = every day at 08:00 local.
        # ``Persistent=true`` makes systemd catch up on missed runs
        # if the laptop was asleep at 08:00 — without it, an
        # 09:30-wake operator silently loses the morning health
        # check.
        unit = generate_daily_timer()

        assert "[Timer]" in unit
        assert "OnCalendar=*-*-* 08:00:00" in unit
        assert "Persistent=true" in unit
        # The timer references the matching .service explicitly so
        # that a future renamed unit doesn't accidentally fire the
        # old service if the operator does a partial upgrade.
        assert f"Unit={DAILY_UNIT_NAME}.service" in unit
        # Required for ``systemctl --user enable`` to actually
        # symlink the timer into the user's ``timers.target.wants``
        # directory. Without ``[Install]`` + ``WantedBy``, ``enable``
        # is a no-op and the timer never starts on login.
        assert "[Install]" in unit
        assert "WantedBy=timers.target" in unit

    def test_hourly_service_uses_one_day_window(self) -> None:
        # Hourly check uses ``--days=1`` — wider window dilutes
        # the hour's signal into noise; narrower (less than a
        # day) is ill-defined for daily-aggregated rules. Same
        # reasoning as slice 1's hourly plist.
        executable = "/usr/local/bin/yadirect-agent"
        log_dir = Path("/tmp/logs")

        unit = generate_hourly_service(executable=executable, log_dir=log_dir)

        assert "Type=oneshot" in unit
        assert f"ExecStart={executable} health --days=1 --json" in unit
        assert f"StandardOutput=append:{log_dir}/hourly.log" in unit
        assert f"StandardError=append:{log_dir}/hourly.err" in unit

    def test_hourly_timer_uses_unit_active_sec(self) -> None:
        # ``OnUnitActiveSec=1h`` = re-fire one hour after the
        # previous run completed. ``OnBootSec=10min`` fires the
        # first run 10 minutes after login (gives the system time
        # to settle, avoids racing other login-time daemons).
        # Together they implement "every hour, starting shortly
        # after login".
        unit = generate_hourly_timer()

        assert "OnBootSec=10min" in unit
        assert "OnUnitActiveSec=1h" in unit
        assert f"Unit={HOURLY_UNIT_NAME}.service" in unit
        assert "[Install]" in unit
        assert "WantedBy=timers.target" in unit

    def test_unit_names_are_pinned(self) -> None:
        # ``systemctl`` looks units up by exact filename. A typo
        # at upgrade time would orphan the previous version's
        # units (still on disk, still scheduled, but the new
        # ``remove`` wouldn't find them). Pin the strings.
        assert DAILY_UNIT_NAME == "yadirect-agent-daily"
        assert HOURLY_UNIT_NAME == "yadirect-agent-hourly"

    def test_executable_path_must_be_absolute(self) -> None:
        # systemd ``ExecStart`` resolves relative to ``$PATH`` if
        # the value isn't absolute, which means the eventual
        # binary depends on whatever ``$PATH`` the user-session
        # bus inherited at login — a footgun. Reject at the
        # boundary, same shape as slice 1.
        with pytest.raises(ValueError, match="absolute"):
            generate_daily_service(
                executable="yadirect-agent",  # not absolute
                log_dir=Path("/tmp/logs"),
            )
        with pytest.raises(ValueError, match="absolute"):
            generate_hourly_service(
                executable="yadirect-agent",
                log_dir=Path("/tmp/logs"),
            )


class TestLinuxSchedulerInstall:
    @pytest.fixture
    def fake_systemctl(self, monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
        """Replace ``systemctl`` with an in-memory spy.

        Each call's args (everything after ``systemctl --user``)
        is appended to the returned list. Tests assert on the
        sequence, not on a real subprocess.
        """
        calls: list[list[str]] = []

        def spy(args: list[str]) -> None:
            calls.append(args)

        monkeypatch.setattr(
            "yadirect_agent.services.scheduler.linux.run_systemctl",
            spy,
        )
        return calls

    def test_install_writes_four_units_and_enables_both_timers(
        self,
        tmp_path: Path,
        fake_systemctl: list[list[str]],
    ) -> None:
        # End-to-end: install writes both .service + .timer pairs
        # into the operator's user-units dir and runs three
        # systemctl commands — one ``daemon-reload`` so systemd
        # picks up the new files, then ``enable --now`` for each
        # timer so it both starts now AND survives logout/login.
        scheduler = LinuxScheduler(
            units_dir=tmp_path / "systemd" / "user",
            log_dir=tmp_path / "logs",
            executable="/usr/local/bin/yadirect-agent",
        )

        result = scheduler.install()

        assert isinstance(result, LinuxInstallResult)
        units = tmp_path / "systemd" / "user"
        assert (units / f"{DAILY_UNIT_NAME}.service").exists()
        assert (units / f"{DAILY_UNIT_NAME}.timer").exists()
        assert (units / f"{HOURLY_UNIT_NAME}.service").exists()
        assert (units / f"{HOURLY_UNIT_NAME}.timer").exists()
        # log_dir created on demand; without it, ``StandardOutput=
        # append:...`` fails the first time the timer fires and
        # the unit ends up in a permanent "failed" state.
        assert (tmp_path / "logs").is_dir()

        # Pin: daemon-reload runs first, then enable --now for
        # both timers. Order matters — enabling before reload
        # would race against systemd's unit cache.
        assert fake_systemctl[0] == ["daemon-reload"]
        enables = fake_systemctl[1:]
        assert ["enable", "--now", f"{DAILY_UNIT_NAME}.timer"] in enables
        assert ["enable", "--now", f"{HOURLY_UNIT_NAME}.timer"] in enables
        assert len(enables) == 2

    def test_install_atomic_via_os_replace(
        self,
        tmp_path: Path,
        fake_systemctl: list[list[str]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Same atomicity contract as slice 1 / ``BusinessProfileStore``:
        # a partial-write crash leaves the original (or no file) —
        # never a half-written unit that systemd would parse-error
        # at the next ``daemon-reload``.
        import os

        replace_calls: list[tuple[str, str]] = []
        original = os.replace

        def spy(src: str | Path, dst: str | Path) -> None:
            replace_calls.append((str(src), str(dst)))
            original(src, dst)

        monkeypatch.setattr("os.replace", spy)

        scheduler = LinuxScheduler(
            units_dir=tmp_path / "systemd" / "user",
            log_dir=tmp_path / "logs",
            executable="/usr/local/bin/yadirect-agent",
        )
        scheduler.install()

        # Four unit files → four replace calls. Each tempfile must
        # be a sibling of its target so the rename is a same-FS
        # atomic operation.
        assert len(replace_calls) == 4
        for src, dst in replace_calls:
            assert src != dst
            assert Path(src).parent == Path(dst).parent

    def test_install_creates_units_dir_on_demand(
        self,
        tmp_path: Path,
        fake_systemctl: list[list[str]],
    ) -> None:
        # Fresh Linux account may not have ``~/.config/systemd/user``
        # yet (systemd doesn't create it until something writes
        # there). Install must mkdir on demand or the very first
        # tempfile write fails.
        target = tmp_path / "fresh" / "systemd" / "user"
        scheduler = LinuxScheduler(
            units_dir=target,
            log_dir=tmp_path / "logs",
            executable="/usr/local/bin/yadirect-agent",
        )
        scheduler.install()
        assert target.is_dir()


class TestLinuxSchedulerStatus:
    @pytest.fixture
    def fake_systemctl(self, monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
        calls: list[list[str]] = []

        def spy(args: list[str]) -> None:
            calls.append(args)

        monkeypatch.setattr(
            "yadirect_agent.services.scheduler.linux.run_systemctl",
            spy,
        )
        return calls

    def test_status_reports_installed_when_all_four_units_present(
        self,
        tmp_path: Path,
        fake_systemctl: list[list[str]],
    ) -> None:
        # ``schedule status`` reads on-disk unit files rather than
        # asking systemctl — same source of truth, no subprocess
        # required, and tests don't have to mock one.
        scheduler = LinuxScheduler(
            units_dir=tmp_path / "systemd" / "user",
            log_dir=tmp_path / "logs",
            executable="/usr/local/bin/yadirect-agent",
        )
        scheduler.install()

        status = scheduler.status()

        assert status.installed is True
        assert status.daily_service_path.exists()
        assert status.daily_timer_path.exists()
        assert status.hourly_service_path.exists()
        assert status.hourly_timer_path.exists()

    def test_status_reports_not_installed_on_fresh_account(self, tmp_path: Path) -> None:
        # No install happened. Status must report "not installed"
        # so the operator's first ``schedule status`` after pip
        # install doesn't lie.
        scheduler = LinuxScheduler(
            units_dir=tmp_path / "systemd" / "user",
            log_dir=tmp_path / "logs",
            executable="/usr/local/bin/yadirect-agent",
        )
        status = scheduler.status()
        assert status.installed is False

    def test_status_reports_partial_when_one_unit_missing(
        self,
        tmp_path: Path,
        fake_systemctl: list[list[str]],
    ) -> None:
        # Half-installed state (e.g. the operator deleted one
        # unit by hand to debug a parse error). All-or-nothing
        # so the operator sees "installed: false" with all four
        # paths printed and can decide whether to reinstall.
        units = tmp_path / "systemd" / "user"
        units.mkdir(parents=True)
        # Three of four files present.
        (units / f"{DAILY_UNIT_NAME}.service").write_text("")
        (units / f"{DAILY_UNIT_NAME}.timer").write_text("")
        (units / f"{HOURLY_UNIT_NAME}.service").write_text("")

        scheduler = LinuxScheduler(
            units_dir=units,
            log_dir=tmp_path / "logs",
            executable="/usr/local/bin/yadirect-agent",
        )
        status = scheduler.status()
        assert status.installed is False


class TestLinuxSchedulerRemove:
    @pytest.fixture
    def fake_systemctl(self, monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
        calls: list[list[str]] = []

        def spy(args: list[str]) -> None:
            calls.append(args)

        monkeypatch.setattr(
            "yadirect_agent.services.scheduler.linux.run_systemctl",
            spy,
        )
        return calls

    def test_remove_disables_both_timers_and_deletes_all_units(
        self,
        tmp_path: Path,
        fake_systemctl: list[list[str]],
    ) -> None:
        scheduler = LinuxScheduler(
            units_dir=tmp_path / "systemd" / "user",
            log_dir=tmp_path / "logs",
            executable="/usr/local/bin/yadirect-agent",
        )
        scheduler.install()
        fake_systemctl.clear()

        scheduler.remove()

        units = tmp_path / "systemd" / "user"
        assert not (units / f"{DAILY_UNIT_NAME}.service").exists()
        assert not (units / f"{DAILY_UNIT_NAME}.timer").exists()
        assert not (units / f"{HOURLY_UNIT_NAME}.service").exists()
        assert not (units / f"{HOURLY_UNIT_NAME}.timer").exists()

        # Pin: ``disable --now`` for each timer (stops the active
        # job AND removes the symlink in ``timers.target.wants``),
        # then ``daemon-reload`` so systemd forgets the unit
        # files we just deleted.
        assert ["disable", "--now", f"{DAILY_UNIT_NAME}.timer"] in fake_systemctl
        assert ["disable", "--now", f"{HOURLY_UNIT_NAME}.timer"] in fake_systemctl
        assert fake_systemctl[-1] == ["daemon-reload"]

    def test_remove_is_idempotent_on_fresh_account(
        self,
        tmp_path: Path,
        fake_systemctl: list[list[str]],
    ) -> None:
        # Operator runs ``schedule remove`` without ever installing.
        # No-op, no error. Same shape as ``KeyringTokenStore.delete``
        # / slice 1's ``MacOSScheduler.remove``.
        scheduler = LinuxScheduler(
            units_dir=tmp_path / "systemd" / "user",
            log_dir=tmp_path / "logs",
            executable="/usr/local/bin/yadirect-agent",
        )
        scheduler.remove()  # no exception

    def test_remove_tolerates_already_disabled_timer(
        self,
        tmp_path: Path,
        fake_systemctl: list[list[str]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # If the operator manually ran ``systemctl --user disable
        # --now yadirect-agent-daily.timer`` between our install
        # and our remove, ``disable`` returns non-zero. We must
        # still proceed to delete the on-disk files so the
        # operator's intent ("be gone") is honoured. Mirrors
        # slice 1's behaviour for ``launchctl unload`` failures.
        import subprocess

        scheduler = LinuxScheduler(
            units_dir=tmp_path / "systemd" / "user",
            log_dir=tmp_path / "logs",
            executable="/usr/local/bin/yadirect-agent",
        )
        scheduler.install()

        def angry_systemctl(args: list[str]) -> None:
            if args[:2] == ["disable", "--now"]:
                raise subprocess.CalledProcessError(returncode=1, cmd=["systemctl", *args])

        monkeypatch.setattr(
            "yadirect_agent.services.scheduler.linux.run_systemctl",
            angry_systemctl,
        )

        scheduler.remove()  # no exception, files gone
        units = tmp_path / "systemd" / "user"
        assert not (units / f"{DAILY_UNIT_NAME}.timer").exists()
        assert not (units / f"{HOURLY_UNIT_NAME}.timer").exists()
