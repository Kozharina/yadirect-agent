"""Tests for Windows Task Scheduler scheduler (M15.6 slice 3).

Mirrors ``test_macos.py`` and ``test_linux.py``: the surface
contract is identical (``install`` / ``status`` / ``remove`` +
atomic writes + a single indirection point for the platform CLI).
The wire format is what differs — two Task Scheduler XML 1.4
documents (one per task) instead of plists or systemd unit files,
``schtasks.exe`` instead of ``launchctl`` / ``systemctl --user``,
and the actions wrap the agent invocation in ``cmd.exe /c "..."``
because Task Scheduler has no ``StandardOutput=append:...``
equivalent.

Two layers:

1. Pure XML generators — no I/O, no subprocess. Round-trip through
   ``xml.etree.ElementTree.fromstring`` to verify the tree shape
   ``schtasks /create /xml`` will accept; pin substring matches on
   the directives we care about (``CalendarTrigger``,
   ``MultipleInstancesPolicy``, ``StartWhenAvailable``,
   ``Repetition`` interval).
2. ``WindowsScheduler.install`` / ``status`` / ``remove`` — replaces
   ``schtasks`` with an in-memory spy via monkeypatch; tempfile
   atomicity verified the same way slices 1 and 2 do it (sibling
   tempfile + ``os.replace``); UTF-16 LE BOM verified by reading
   the bytes back.

Why UTF-16 LE with BOM: Task Scheduler's GUI exporter emits XML
in UTF-16 LE with BOM. ``schtasks /create /xml`` accepts UTF-8 too
on modern Windows (10/11) but UTF-16 is the canonical interchange
format and survives every legacy schtasks version. We pin the
encoding so a future refactor that "just uses UTF-8" doesn't
silently break operators on older Windows builds.

Why platform-agnostic tests: every test in this file runs on macOS
/ Linux dev boxes and CI alike. Real schtasks invocations and
real Windows path semantics are out of scope — they need an actual
Windows runner, which CI doesn't provision today. Slice 3 ships
the same shape slice 2 did (in-memory spy + tempfile checks); a
future Windows-CI lane runs the integration verification.
"""

from __future__ import annotations

import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from yadirect_agent.services.scheduler.windows import (
    DAILY_TASK_NAME,
    HOURLY_TASK_NAME,
    TASK_NAMESPACE,
    WindowsInstallResult,
    WindowsScheduler,
    generate_daily_xml,
    generate_hourly_xml,
)

# Namespace map for ElementTree XPath round-trips. Task XML 1.4
# uses a single default namespace, so every find() needs the
# ``t:`` prefix bound here.
_NS = {"t": TASK_NAMESPACE}


class TestUnitGeneration:
    def test_daily_xml_round_trips_via_elementtree(self) -> None:
        # Task Scheduler parses XML strictly: a malformed document
        # makes ``schtasks /create /xml`` exit non-zero with a
        # cryptic error. Round-tripping through stdlib
        # ElementTree catches structural regressions (unclosed
        # tags, wrong namespace, invalid nesting) before they
        # reach an operator's machine.
        executable = r"C:\Users\anna\venv\Scripts\yadirect-agent.exe"
        log_dir = Path(r"C:\Users\anna\AppData\Local\yadirect-agent\logs")

        xml = generate_daily_xml(executable=executable, log_dir=log_dir)

        # Strip the XML declaration line for ET.fromstring (it
        # accepts a declaration but it's not required for parsing).
        tree = ET.fromstring(xml)
        # Root must be ``<Task version="1.4" xmlns="...">``.
        assert tree.tag == f"{{{TASK_NAMESPACE}}}Task"
        assert tree.attrib["version"] == "1.4"

    def test_daily_xml_pins_calendar_trigger_at_eight(self) -> None:
        # ``CalendarTrigger`` + ``ScheduleByDay`` with
        # ``DaysInterval=1`` is the Task Scheduler idiom for "every
        # day at the same wall-clock time". The wall-clock is
        # encoded in ``StartBoundary``'s time component (08:00:00).
        # Mirrors slice 1's ``StartCalendarInterval={Hour:8,Minute:0}``
        # and slice 2's ``OnCalendar=*-*-* 08:00:00``.
        xml = generate_daily_xml(
            executable=r"C:\opt\yadirect-agent.exe",
            log_dir=Path(r"C:\logs"),
        )
        tree = ET.fromstring(xml)

        trigger = tree.find(".//t:CalendarTrigger", _NS)
        assert trigger is not None
        # StartBoundary time component locks the daily fire to 08:00.
        # Date component is a fixed past date (2024-01-01) so the XML
        # is deterministic across re-installs — re-running install
        # produces identical bytes, the atomic write becomes a no-op
        # rather than racing systemd-style cache inconsistencies.
        start = trigger.find("t:StartBoundary", _NS)
        assert start is not None and start.text is not None
        assert start.text.endswith("T08:00:00")
        # Schedule cadence: every day.
        schedule = trigger.find("t:ScheduleByDay/t:DaysInterval", _NS)
        assert schedule is not None
        assert schedule.text == "1"

    def test_daily_xml_uses_seven_day_health_window(self) -> None:
        # Daily run summarises the past week — ``health --days=7``.
        # Mirrors slice 1's daily plist and slice 2's daily service.
        xml = generate_daily_xml(
            executable=r"C:\opt\yadirect-agent.exe",
            log_dir=Path(r"C:\logs"),
        )
        tree = ET.fromstring(xml)

        args_el = tree.find(".//t:Arguments", _NS)
        assert args_el is not None and args_el.text is not None
        # ET un-escapes &gt; back to > on parse, so we can match
        # the un-escaped form here.
        assert "health --days=7 --json" in args_el.text
        # Logs land in a stable spot the operator can ``Get-Content
        # -Wait`` (PowerShell tail). ``>>`` (append) keeps the log
        # across runs rather than truncating at every fire.
        assert ">>" in args_el.text
        assert "daily.log" in args_el.text
        assert "daily.err" in args_el.text

    def test_hourly_xml_pins_time_trigger_with_hourly_repetition(self) -> None:
        # Hourly cadence is encoded as a ``TimeTrigger`` with a
        # ``Repetition`` of ``PT1H`` (ISO 8601 one-hour duration).
        # Mirrors slice 1's ``StartInterval=3600`` and slice 2's
        # ``OnUnitActiveSec=1h``.
        xml = generate_hourly_xml(
            executable=r"C:\opt\yadirect-agent.exe",
            log_dir=Path(r"C:\logs"),
        )
        tree = ET.fromstring(xml)

        trigger = tree.find(".//t:TimeTrigger", _NS)
        assert trigger is not None
        rep_interval = trigger.find("t:Repetition/t:Interval", _NS)
        assert rep_interval is not None
        # ISO 8601 ``PT1H`` = period of 1 hour. ``PT60M`` would
        # also work but ``PT1H`` is what every Task Scheduler XML
        # template uses; consistency over cleverness.
        assert rep_interval.text == "PT1H"

    def test_hourly_xml_uses_one_day_window(self) -> None:
        # Hourly check uses ``--days=1`` — wider window dilutes
        # the hour's signal into noise; narrower (less than a
        # day) is ill-defined for daily-aggregated rules. Same
        # reasoning as slices 1 and 2.
        xml = generate_hourly_xml(
            executable=r"C:\opt\yadirect-agent.exe",
            log_dir=Path(r"C:\logs"),
        )
        tree = ET.fromstring(xml)

        args_el = tree.find(".//t:Arguments", _NS)
        assert args_el is not None and args_el.text is not None
        assert "health --days=1 --json" in args_el.text
        assert "hourly.log" in args_el.text
        assert "hourly.err" in args_el.text

    def test_xml_pins_settings_for_resilience(self) -> None:
        # Five Settings directives that together implement the
        # "fire even if the laptop was asleep / on battery / busy"
        # contract slices 1 and 2 give:
        #
        # - ``MultipleInstancesPolicy=IgnoreNew``: if the previous
        #   run is still going when the next trigger fires, drop the
        #   new run. Health checks are short; overlap is a smell.
        # - ``DisallowStartIfOnBatteries=false``: fire on battery —
        #   running the agent shouldn't gate on AC power.
        # - ``StopIfGoingOnBatteries=false``: don't kill an
        #   in-flight run if the laptop unplugs mid-check.
        # - ``StartWhenAvailable=true``: fire missed runs when the
        #   machine wakes (analogue of systemd ``Persistent=true``).
        # - ``Enabled=true``: the task is active immediately on
        #   register; no separate ``schtasks /change /enable`` call.
        for xml in (
            generate_daily_xml(executable=r"C:\opt\yadirect-agent.exe", log_dir=Path(r"C:\logs")),
            generate_hourly_xml(executable=r"C:\opt\yadirect-agent.exe", log_dir=Path(r"C:\logs")),
        ):
            tree = ET.fromstring(xml)
            settings = tree.find("t:Settings", _NS)
            assert settings is not None
            # Each ``find`` returns an Element with text but no
            # children; ``Element`` is falsy when it has no children
            # (Python stdlib quirk), so the explicit ``is not None``
            # check is required — ``or fallback`` would silently
            # take the wrong branch.
            for tag, expected in (
                ("MultipleInstancesPolicy", "IgnoreNew"),
                ("DisallowStartIfOnBatteries", "false"),
                ("StopIfGoingOnBatteries", "false"),
                ("StartWhenAvailable", "true"),
                ("Enabled", "true"),
            ):
                el = settings.find(f"t:{tag}", _NS)
                assert el is not None, f"missing <{tag}>"
                assert el.text == expected, f"<{tag}> wrong: {el.text!r}"

    def test_xml_action_wraps_executable_in_cmd_exe(self) -> None:
        # Task Scheduler has no ``StandardOutput=append:...``
        # equivalent (slice 1's launchd / slice 2's systemd both
        # offer one). The portable workaround is to invoke
        # ``cmd.exe /c "<cmdline> >> log 2>> err"``: cmd interprets
        # ``>>`` and ``2>>`` as append redirects after Task
        # Scheduler hands it the full argument string. Pin the
        # cmd.exe wrapper so a regression that swapped to a bare
        # ``<Command>yadirect-agent.exe</Command>`` invocation
        # silently loses log capture.
        xml = generate_daily_xml(
            executable=r"C:\opt\yadirect-agent.exe",
            log_dir=Path(r"C:\logs"),
        )
        tree = ET.fromstring(xml)

        cmd_el = tree.find(".//t:Exec/t:Command", _NS)
        assert cmd_el is not None
        # Absolute path to cmd.exe — same defence-in-depth as
        # slices 1+2 give for launchctl/systemctl: a relative
        # ``cmd.exe`` would resolve against whatever PATH Task
        # Scheduler inherited at boot, which an operator can't
        # control.
        assert cmd_el.text == r"C:\Windows\System32\cmd.exe"

        args_el = tree.find(".//t:Exec/t:Arguments", _NS)
        assert args_el is not None and args_el.text is not None
        # ``/c`` runs the quoted command and exits — vs ``/k``
        # which keeps cmd open. We want exit-on-completion so
        # Task Scheduler can mark the run finished.
        assert args_el.text.startswith("/c ")
        # The agent executable lives inside the quoted command
        # string, NOT as a bare argument — otherwise cmd would
        # fail to parse the ``>>`` redirect as belonging to it.
        assert r"C:\opt\yadirect-agent.exe" in args_el.text

    def test_task_names_are_pinned(self) -> None:
        # ``schtasks /Delete /TN <name>`` looks up tasks by exact
        # name. A typo at upgrade time would orphan the previous
        # version's tasks (still scheduled in Task Store, but our
        # ``remove`` wouldn't find them). Pin the strings, mirroring
        # slice 2's unit-name convention.
        assert DAILY_TASK_NAME == "yadirect-agent-daily"
        assert HOURLY_TASK_NAME == "yadirect-agent-hourly"

    def test_executable_path_must_be_absolute(self) -> None:
        # cmd.exe inside Task Scheduler resolves a relative
        # executable against ``C:\Windows\System32`` (its CWD when
        # launched by Task Scheduler), not the operator's. Relative
        # paths silently fail to launch with a "command not found"
        # buried in Task Scheduler's Last Run Result. Reject at the
        # boundary — same shape as slices 1 and 2.
        with pytest.raises(ValueError, match="absolute"):
            generate_daily_xml(
                executable="yadirect-agent.exe",  # not absolute
                log_dir=Path(r"C:\logs"),
            )
        with pytest.raises(ValueError, match="absolute"):
            generate_hourly_xml(
                executable="yadirect-agent.exe",
                log_dir=Path(r"C:\logs"),
            )


class TestWindowsSchedulerInstall:
    @pytest.fixture
    def fake_schtasks(self, monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
        """Replace ``schtasks`` with an in-memory spy.

        Each call's args (everything after ``schtasks.exe``) is
        appended to the returned list. Tests assert on the
        sequence rather than launching a real subprocess (which
        would fail on macOS / Linux dev boxes anyway).
        """
        calls: list[list[str]] = []

        def spy(args: list[str]) -> None:
            calls.append(args)

        monkeypatch.setattr(
            "yadirect_agent.services.scheduler.windows.run_schtasks",
            spy,
        )
        return calls

    def test_install_writes_two_xml_and_creates_both_tasks(
        self,
        tmp_path: Path,
        fake_schtasks: list[list[str]],
    ) -> None:
        # End-to-end: install writes both XML files into the
        # operator's schedule dir and runs two ``schtasks /Create
        # /XML`` invocations — one per task. ``/F`` (force) means
        # "overwrite if a task with the same name already exists",
        # which makes re-running install idempotent (operator can
        # change the executable path and reinstall without manual
        # cleanup).
        scheduler = WindowsScheduler(
            xml_dir=tmp_path / "schedule",
            log_dir=tmp_path / "logs",
            executable=r"C:\opt\yadirect-agent.exe",
        )

        result = scheduler.install()

        assert isinstance(result, WindowsInstallResult)
        schedule_dir = tmp_path / "schedule"
        assert (schedule_dir / f"{DAILY_TASK_NAME}.xml").exists()
        assert (schedule_dir / f"{HOURLY_TASK_NAME}.xml").exists()
        # log_dir created on demand; if missing, the first cmd.exe
        # redirect inside the action would fail with a cryptic
        # "the system cannot find the path specified" error in
        # Task Scheduler's history.
        assert (tmp_path / "logs").is_dir()

        # Pin: two ``/Create`` invocations, one per task. ``/F``
        # required so re-install overwrites without prompting.
        assert len(fake_schtasks) == 2
        daily_call = next(c for c in fake_schtasks if DAILY_TASK_NAME in c)
        hourly_call = next(c for c in fake_schtasks if HOURLY_TASK_NAME in c)
        # Argument shape: ``/Create /TN <name> /XML <path> /F``.
        # Task Scheduler accepts both ``/create`` and ``/Create``;
        # we pick the capitalised form because Microsoft's official
        # docs and PowerShell ``Register-ScheduledTask`` examples
        # use it.
        assert "/Create" in daily_call
        assert "/TN" in daily_call
        assert "/XML" in daily_call
        assert "/F" in daily_call
        assert str(schedule_dir / f"{DAILY_TASK_NAME}.xml") in daily_call
        assert "/Create" in hourly_call
        assert str(schedule_dir / f"{HOURLY_TASK_NAME}.xml") in hourly_call

    def test_install_writes_xml_as_utf16_le_with_bom(
        self,
        tmp_path: Path,
        fake_schtasks: list[list[str]],
    ) -> None:
        # Microsoft's Task Scheduler GUI exporter emits XML in
        # UTF-16 LE with BOM, and that's the canonical format
        # ``schtasks /Create /XML`` expects. UTF-8 works on
        # Windows 10/11 too, but UTF-16 is the safe bet across
        # every legacy schtasks version we might encounter.
        # Verify the on-disk bytes start with the LE BOM and
        # decode round-trip cleanly.
        scheduler = WindowsScheduler(
            xml_dir=tmp_path / "schedule",
            log_dir=tmp_path / "logs",
            executable=r"C:\opt\yadirect-agent.exe",
        )
        scheduler.install()

        daily_bytes = (tmp_path / "schedule" / f"{DAILY_TASK_NAME}.xml").read_bytes()
        # UTF-16 LE BOM = 0xFF 0xFE.
        assert daily_bytes.startswith(b"\xff\xfe")
        # Strip BOM and decode; the result must be the same XML
        # ``generate_daily_xml`` returned (sanity check that we
        # didn't accidentally double-encode).
        decoded = daily_bytes[2:].decode("utf-16-le")
        assert "<Task" in decoded
        assert "yadirect-agent daily health check" in decoded

    def test_install_atomic_via_os_replace(
        self,
        tmp_path: Path,
        fake_schtasks: list[list[str]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Same atomicity contract as slices 1 and 2: a partial-
        # write crash leaves the original (or no file) — never a
        # half-written XML that Task Scheduler would parse-error
        # on at the next ``schtasks /Create /XML``. Verify each
        # write goes through ``os.replace`` from a sibling
        # tempfile.
        import os

        replace_calls: list[tuple[str, str]] = []
        original = os.replace

        def spy(src: str | Path, dst: str | Path) -> None:
            replace_calls.append((str(src), str(dst)))
            original(src, dst)

        monkeypatch.setattr("os.replace", spy)

        scheduler = WindowsScheduler(
            xml_dir=tmp_path / "schedule",
            log_dir=tmp_path / "logs",
            executable=r"C:\opt\yadirect-agent.exe",
        )
        scheduler.install()

        # Two XML files → two replace calls. Each tempfile must
        # be a sibling of its target so the rename is a same-FS
        # atomic operation.
        assert len(replace_calls) == 2
        for src, dst in replace_calls:
            assert src != dst
            assert Path(src).parent == Path(dst).parent

    def test_install_creates_xml_dir_on_demand(
        self,
        tmp_path: Path,
        fake_schtasks: list[list[str]],
    ) -> None:
        # Fresh Windows account may not have
        # ``%LOCALAPPDATA%\yadirect-agent\schedule`` yet. Install
        # must mkdir on demand or the very first tempfile write
        # fails with FileNotFoundError.
        target = tmp_path / "fresh" / "schedule"
        scheduler = WindowsScheduler(
            xml_dir=target,
            log_dir=tmp_path / "logs",
            executable=r"C:\opt\yadirect-agent.exe",
        )
        scheduler.install()
        assert target.is_dir()


class TestWindowsSchedulerStatus:
    @pytest.fixture
    def fake_schtasks(self, monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
        calls: list[list[str]] = []

        def spy(args: list[str]) -> None:
            calls.append(args)

        monkeypatch.setattr(
            "yadirect_agent.services.scheduler.windows.run_schtasks",
            spy,
        )
        return calls

    def test_status_reports_installed_when_both_xml_present(
        self,
        tmp_path: Path,
        fake_schtasks: list[list[str]],
    ) -> None:
        # ``schedule status`` reads the on-disk XML files rather
        # than asking schtasks — same source-of-truth pattern as
        # slices 1 and 2 (read plists / unit files, not launchctl
        # / systemctl). Avoids a subprocess on the read path and
        # makes status work in test environments.
        scheduler = WindowsScheduler(
            xml_dir=tmp_path / "schedule",
            log_dir=tmp_path / "logs",
            executable=r"C:\opt\yadirect-agent.exe",
        )
        scheduler.install()

        status = scheduler.status()

        assert status.installed is True
        assert status.daily_xml_path.exists()
        assert status.hourly_xml_path.exists()

    def test_status_reports_not_installed_on_fresh_account(self, tmp_path: Path) -> None:
        # No install happened. Status must report "not installed"
        # so the operator's first ``schedule status`` after a
        # fresh pip install doesn't lie.
        scheduler = WindowsScheduler(
            xml_dir=tmp_path / "schedule",
            log_dir=tmp_path / "logs",
            executable=r"C:\opt\yadirect-agent.exe",
        )
        status = scheduler.status()
        assert status.installed is False

    def test_status_reports_partial_when_only_one_xml_present(
        self,
        tmp_path: Path,
        fake_schtasks: list[list[str]],
    ) -> None:
        # Half-installed state (e.g. operator deleted one XML by
        # hand to debug a parse error). All-or-nothing so the
        # operator sees "installed: false" with both paths printed
        # and can decide whether to reinstall.
        schedule_dir = tmp_path / "schedule"
        schedule_dir.mkdir(parents=True)
        # Only the daily XML present; hourly missing.
        (schedule_dir / f"{DAILY_TASK_NAME}.xml").write_bytes(b"")

        scheduler = WindowsScheduler(
            xml_dir=schedule_dir,
            log_dir=tmp_path / "logs",
            executable=r"C:\opt\yadirect-agent.exe",
        )
        status = scheduler.status()
        assert status.installed is False


class TestWindowsSchedulerRemove:
    @pytest.fixture
    def fake_schtasks(self, monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
        calls: list[list[str]] = []

        def spy(args: list[str]) -> None:
            calls.append(args)

        monkeypatch.setattr(
            "yadirect_agent.services.scheduler.windows.run_schtasks",
            spy,
        )
        return calls

    def test_remove_deletes_both_tasks_and_xml_files(
        self,
        tmp_path: Path,
        fake_schtasks: list[list[str]],
    ) -> None:
        scheduler = WindowsScheduler(
            xml_dir=tmp_path / "schedule",
            log_dir=tmp_path / "logs",
            executable=r"C:\opt\yadirect-agent.exe",
        )
        scheduler.install()
        fake_schtasks.clear()

        scheduler.remove()

        schedule_dir = tmp_path / "schedule"
        assert not (schedule_dir / f"{DAILY_TASK_NAME}.xml").exists()
        assert not (schedule_dir / f"{HOURLY_TASK_NAME}.xml").exists()
        # Pin: two ``/Delete`` invocations, one per task. ``/F``
        # required so schtasks doesn't prompt "are you sure?".
        # Task name first (``/TN <name>``), then ``/F``; this
        # mirrors what ``schtasks /Delete -?`` documents.
        delete_calls = [c for c in fake_schtasks if "/Delete" in c]
        assert len(delete_calls) == 2
        for call in delete_calls:
            assert "/TN" in call
            assert "/F" in call
        names = {c[c.index("/TN") + 1] for c in delete_calls}
        assert names == {DAILY_TASK_NAME, HOURLY_TASK_NAME}

    def test_remove_is_idempotent_on_fresh_account(
        self,
        tmp_path: Path,
        fake_schtasks: list[list[str]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Operator runs ``schedule remove`` without ever installing.
        # No exception, the on-disk state is "no files" and that's
        # a success. Same shape as slices 1 and 2.
        #
        # On a fresh account, ``schtasks /Delete`` returns non-zero
        # ("the specified task name does not exist"). We tolerate
        # that exactly the same way slice 2 tolerates ``systemctl
        # disable`` failure: log a warning, proceed to file
        # cleanup.
        def angry_schtasks(args: list[str]) -> None:
            if "/Delete" in args:
                raise subprocess.CalledProcessError(returncode=1, cmd=["schtasks.exe", *args])

        monkeypatch.setattr(
            "yadirect_agent.services.scheduler.windows.run_schtasks",
            angry_schtasks,
        )

        scheduler = WindowsScheduler(
            xml_dir=tmp_path / "schedule",
            log_dir=tmp_path / "logs",
            executable=r"C:\opt\yadirect-agent.exe",
        )
        scheduler.remove()  # no exception

    def test_remove_tolerates_already_deleted_task(
        self,
        tmp_path: Path,
        fake_schtasks: list[list[str]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # If the operator manually ran ``schtasks /Delete /TN
        # yadirect-agent-daily /F`` between our install and our
        # remove, the next ``/Delete`` returns non-zero. We must
        # still proceed to delete the on-disk XML so the
        # operator's intent ("be gone") is honoured. Mirrors
        # slice 1's ``launchctl unload`` failure tolerance and
        # slice 2's ``systemctl disable`` failure tolerance.
        scheduler = WindowsScheduler(
            xml_dir=tmp_path / "schedule",
            log_dir=tmp_path / "logs",
            executable=r"C:\opt\yadirect-agent.exe",
        )
        scheduler.install()

        def angry_schtasks(args: list[str]) -> None:
            if "/Delete" in args:
                raise subprocess.CalledProcessError(returncode=1, cmd=["schtasks.exe", *args])

        monkeypatch.setattr(
            "yadirect_agent.services.scheduler.windows.run_schtasks",
            angry_schtasks,
        )

        scheduler.remove()  # no exception, files gone
        schedule_dir = tmp_path / "schedule"
        assert not (schedule_dir / f"{DAILY_TASK_NAME}.xml").exists()
        assert not (schedule_dir / f"{HOURLY_TASK_NAME}.xml").exists()
