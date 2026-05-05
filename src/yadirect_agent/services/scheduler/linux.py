"""Linux ``systemd --user`` scheduler (M15.6 slice 2).

Two scheduled jobs, mirroring slice 1 (macOS):

- ``yadirect-agent-daily.timer`` fires daily at 08:00 local time and
  triggers ``yadirect-agent-daily.service``, which runs
  ``yadirect-agent health --days=7 --json``.
- ``yadirect-agent-hourly.timer`` re-fires one hour after each
  previous run completes (with a 10-minute grace after login) and
  triggers ``yadirect-agent-hourly.service``, which runs
  ``yadirect-agent health --days=1 --json``.

Why systemd ``--user`` and not a system-level ``/etc/systemd/system``
unit: the agent runs as the operator's user (it owns the
keychain-backed token, the local audit log, and the venv). A
system-level unit would either need a hard-coded ``User=``
directive or the operator's password to install — neither
acceptable for the "Anna installs from PyPI" path.

Why ``Persistent=true`` on the daily timer: laptops sleep at 08:00.
Without ``Persistent``, a missed run is silently dropped; with it,
systemd fires the catch-up the next time the user-bus is alive.
The hourly timer doesn't need it — the next ``OnUnitActiveSec=1h``
window is at most an hour away anyway.

Why ``Type=oneshot`` on the services: systemd's default ``simple``
type assumes a long-running daemon and will log spurious
"started/stopped" pairs every cron tick — confusing to read in
``journalctl``. ``oneshot`` says "fire-and-exit"; cleanest match
for a health check.

Why log to files (``StandardOutput=append:...``) instead of the
journal: operators need a path they can ``tail -f`` without
remembering ``journalctl --user -u yadirect-agent-daily``. The
journal still gets the structured logs from the agent itself
(via structlog → stderr → systemd → journal); the file is a
supplementary, human-friendly view aligned with where slice 1
puts macOS logs.

All ``systemctl`` invocations go through ``run_systemctl`` so
tests can replace it with an in-memory spy. Production
implementation is a single ``subprocess.run`` call. The hard-coded
``/usr/bin/systemctl`` path matches every mainstream distro
(Debian, Ubuntu, Fedora, Arch); operators on NixOS or other
non-FHS systems where ``systemctl`` lives elsewhere can override
``run_systemctl`` directly — same escape hatch slice 1 documents
for ``run_launchctl``.

Atomicity: each unit file is written via tempfile + ``os.replace``
in the same directory as the target. A partial-write crash leaves
the original unit (or no file) — never a half-written stanza that
``daemon-reload`` would parse-error on.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import structlog

_log = structlog.get_logger(component="services.scheduler.linux")

DAILY_UNIT_NAME = "yadirect-agent-daily"
HOURLY_UNIT_NAME = "yadirect-agent-hourly"


def run_systemctl(args: list[str]) -> None:
    """Invoke ``systemctl --user`` with ``args``. Indirection point for tests.

    Production: ``subprocess.run(["/usr/bin/systemctl", "--user",
    *args], check=True, capture_output=True)``. Tests monkeypatch
    this symbol with an in-memory spy so no real subprocess runs.

    A non-zero exit propagates as ``CalledProcessError``. ``capture_output``
    surfaces ``systemctl`` stderr to the operator via the audit log;
    swallowing failures would mean the timer is not actually scheduled
    and pretending success would be a worse failure mode than a loud raise.

    Note: ``--user`` is not part of ``args`` — every call this module
    makes targets the user bus, so the flag is structural, not
    per-call. Tests assert against the post-``--user`` argv only.
    """
    # ``/usr/bin/systemctl`` is the standard FHS location on every
    # mainstream distro. Absolute path defeats the bandit S607
    # partial-path warning. NixOS and other non-FHS users can
    # override this function directly (lazy import or monkeypatch
    # at startup) — same escape hatch slice 1 offers for launchctl.
    subprocess.run(  # noqa: S603 — args constructed in this module, no shell
        ["/usr/bin/systemctl", "--user", *args],
        check=True,
        capture_output=True,
    )


def _validate_executable(executable: str) -> None:
    if not Path(executable).is_absolute():
        msg = (
            "executable must be an absolute path; systemd ExecStart resolves "
            f"a relative value against $PATH inherited at user-bus start, got {executable!r}"
        )
        raise ValueError(msg)


def generate_daily_service(*, executable: str, log_dir: Path) -> str:
    """Daily 08:00 health unit (one-shot, runs 7-day check).

    Returns the unit-file text systemd will load. The matching
    ``.timer`` (see ``generate_daily_timer``) is what triggers it.
    """
    _validate_executable(executable)
    return (
        "[Unit]\n"
        "Description=yadirect-agent daily health check (7-day window)\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"ExecStart={executable} health --days=7 --json\n"
        f"StandardOutput=append:{log_dir}/daily.log\n"
        f"StandardError=append:{log_dir}/daily.err\n"
    )


def generate_daily_timer() -> str:
    """Daily 08:00 timer (catches up missed runs via ``Persistent``).

    No parameters: the timer references its sibling service by name
    (``Unit=yadirect-agent-daily.service``) and the schedule is
    fixed by product spec.
    """
    return (
        "[Unit]\n"
        "Description=Run yadirect-agent health check daily at 08:00 local time\n"
        "\n"
        "[Timer]\n"
        "OnCalendar=*-*-* 08:00:00\n"
        "Persistent=true\n"
        f"Unit={DAILY_UNIT_NAME}.service\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )


def generate_hourly_service(*, executable: str, log_dir: Path) -> str:
    """Hourly health unit (one-shot, today's 1-day window)."""
    _validate_executable(executable)
    return (
        "[Unit]\n"
        "Description=yadirect-agent hourly health check (today's window)\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"ExecStart={executable} health --days=1 --json\n"
        f"StandardOutput=append:{log_dir}/hourly.log\n"
        f"StandardError=append:{log_dir}/hourly.err\n"
    )


def generate_hourly_timer() -> str:
    """Hourly timer (10 min after login + every hour after each run).

    ``OnBootSec=10min`` gives the system time to settle after login
    so we don't race the OAuth token refresher or other startup
    daemons. ``OnUnitActiveSec=1h`` re-fires one hour after the
    previous service completes, which is the systemd idiom for
    "every hour from now on" (vs ``OnCalendar=hourly`` which fires
    on wall-clock minute 0 of every hour and would coincide with
    every other hourly job on the system).
    """
    return (
        "[Unit]\n"
        "Description=Run yadirect-agent health check every hour\n"
        "\n"
        "[Timer]\n"
        "OnBootSec=10min\n"
        "OnUnitActiveSec=1h\n"
        f"Unit={HOURLY_UNIT_NAME}.service\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )


@dataclass(frozen=True)
class LinuxInstallResult:
    """What ``LinuxScheduler.install`` returns to the CLI.

    The CLI surfaces these paths to the operator so they can
    ``tail -f`` the logs or ``systemctl --user disable`` manually
    if something goes wrong.
    """

    daily_service_path: Path
    daily_timer_path: Path
    hourly_service_path: Path
    hourly_timer_path: Path
    log_dir: Path


@dataclass(frozen=True)
class ScheduleStatus:
    """What ``LinuxScheduler.status`` returns.

    ``installed=True`` requires ALL FOUR units present. Any
    partial state (one missing) reads as ``installed=False`` so
    the operator's ``schedule status`` after a partial / failed
    install doesn't lie.
    """

    installed: bool
    daily_service_path: Path
    daily_timer_path: Path
    hourly_service_path: Path
    hourly_timer_path: Path


class LinuxScheduler:
    """Lifecycle for the daily + hourly user-level systemd timers.

    Constructed with explicit paths (``units_dir``, ``log_dir``)
    so tests don't depend on the real home directory; the CLI
    passes ``$XDG_CONFIG_HOME/systemd/user`` (default
    ``~/.config/systemd/user``) and ``$XDG_STATE_HOME/yadirect-agent/logs``
    (default ``~/.local/state/yadirect-agent/logs``) in production.
    """

    def __init__(
        self,
        *,
        units_dir: Path,
        log_dir: Path,
        executable: str,
    ) -> None:
        _validate_executable(executable)
        self._units_dir = units_dir
        self._log_dir = log_dir
        self._executable = executable

    @property
    def daily_service_path(self) -> Path:
        return self._units_dir / f"{DAILY_UNIT_NAME}.service"

    @property
    def daily_timer_path(self) -> Path:
        return self._units_dir / f"{DAILY_UNIT_NAME}.timer"

    @property
    def hourly_service_path(self) -> Path:
        return self._units_dir / f"{HOURLY_UNIT_NAME}.service"

    @property
    def hourly_timer_path(self) -> Path:
        return self._units_dir / f"{HOURLY_UNIT_NAME}.timer"

    def _all_unit_paths(self) -> tuple[Path, Path, Path, Path]:
        return (
            self.daily_service_path,
            self.daily_timer_path,
            self.hourly_service_path,
            self.hourly_timer_path,
        )

    # -- Writes ----------------------------------------------------------

    def install(self) -> LinuxInstallResult:
        """Write all four units atomically and ``enable --now`` both timers.

        Order matters: the unit files must exist on disk before
        ``daemon-reload``, and ``daemon-reload`` must run before
        ``enable`` so systemd has the new units in its in-memory
        cache. Calling ``enable`` against a unit systemd doesn't
        know about yet returns "Failed to enable unit: Unit file
        ... does not exist."
        """
        self._units_dir.mkdir(parents=True, exist_ok=True)
        self._log_dir.mkdir(parents=True, exist_ok=True)

        self._atomic_write(
            self.daily_service_path,
            generate_daily_service(executable=self._executable, log_dir=self._log_dir),
        )
        self._atomic_write(self.daily_timer_path, generate_daily_timer())
        self._atomic_write(
            self.hourly_service_path,
            generate_hourly_service(executable=self._executable, log_dir=self._log_dir),
        )
        self._atomic_write(self.hourly_timer_path, generate_hourly_timer())

        run_systemctl(["daemon-reload"])
        run_systemctl(["enable", "--now", f"{DAILY_UNIT_NAME}.timer"])
        run_systemctl(["enable", "--now", f"{HOURLY_UNIT_NAME}.timer"])

        _log.info(
            "scheduler.linux.installed",
            daily_service=str(self.daily_service_path),
            daily_timer=str(self.daily_timer_path),
            hourly_service=str(self.hourly_service_path),
            hourly_timer=str(self.hourly_timer_path),
        )
        return LinuxInstallResult(
            daily_service_path=self.daily_service_path,
            daily_timer_path=self.daily_timer_path,
            hourly_service_path=self.hourly_service_path,
            hourly_timer_path=self.hourly_timer_path,
            log_dir=self._log_dir,
        )

    def remove(self) -> None:
        """Idempotent removal: disable each timer + delete each unit + reload.

        Order matters in the other direction: ``disable --now``
        first so systemd stops the active timer and removes the
        ``timers.target.wants`` symlink, THEN delete the unit
        files, THEN ``daemon-reload`` so systemd forgets the
        deleted units. Reload-before-delete would be a no-op (the
        files still exist at reload time); delete-before-disable
        would orphan the wants-symlink.

        Same shape as slice 1 / ``KeyringTokenStore.delete``: a
        no-op on a fresh account is the success path, not an
        error. ``CalledProcessError`` from ``disable`` is logged
        and ignored — the most likely cause is the operator
        already disabled the timer manually, and the on-disk
        cleanup must still proceed so their intent is honoured.
        """
        for timer_unit in (f"{DAILY_UNIT_NAME}.timer", f"{HOURLY_UNIT_NAME}.timer"):
            try:
                run_systemctl(["disable", "--now", timer_unit])
            except subprocess.CalledProcessError:
                _log.warning(
                    "scheduler.linux.disable_failed",
                    unit=timer_unit,
                    note="continuing with file delete",
                )

        any_deleted = False
        for path in self._all_unit_paths():
            if path.exists():
                path.unlink(missing_ok=True)
                any_deleted = True

        # daemon-reload only if we actually changed the on-disk
        # state. Skipping it on a fresh-account no-op keeps the
        # operator's ``schedule remove`` truly silent (no spurious
        # systemctl audit-log entries).
        if any_deleted:
            try:
                run_systemctl(["daemon-reload"])
            except subprocess.CalledProcessError:
                _log.warning(
                    "scheduler.linux.daemon_reload_failed",
                    note="files already removed; on-disk state matches operator intent",
                )

        _log.info("scheduler.linux.removed", units_dir=str(self._units_dir))

    # -- Reads -----------------------------------------------------------

    def status(self) -> ScheduleStatus:
        """Read-only state probe: are all four unit files present?"""
        all_present = all(path.exists() for path in self._all_unit_paths())
        return ScheduleStatus(
            installed=all_present,
            daily_service_path=self.daily_service_path,
            daily_timer_path=self.daily_timer_path,
            hourly_service_path=self.hourly_service_path,
            hourly_timer_path=self.hourly_timer_path,
        )

    # -- Internals -------------------------------------------------------

    @staticmethod
    def _atomic_write(target: Path, payload: str) -> None:
        """Same atomicity contract as slice 1's ``MacOSScheduler._atomic_write``.

        Tempfile sits in the same directory as the target so
        ``os.replace`` is a same-filesystem atomic rename. Failed
        rename cleans the tempfile and re-raises so callers know
        the write did not happen.
        """
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
            encoding="utf-8",
        ) as tmp:
            tmp.write(payload)
            tmp_path = Path(tmp.name)
        try:
            os.replace(tmp_path, target)
        except OSError:
            tmp_path.unlink(missing_ok=True)
            raise


__all__ = [
    "DAILY_UNIT_NAME",
    "HOURLY_UNIT_NAME",
    "LinuxInstallResult",
    "LinuxScheduler",
    "ScheduleStatus",
    "generate_daily_service",
    "generate_daily_timer",
    "generate_hourly_service",
    "generate_hourly_timer",
    "run_systemctl",
]
