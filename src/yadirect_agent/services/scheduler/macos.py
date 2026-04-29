"""macOS LaunchAgent scheduler (M15.6 slice 1).

Two scheduled jobs:
- ``com.yadirect-agent.daily`` — fires at 08:00 local time, runs
  ``yadirect-agent health --days=7 --json``.
- ``com.yadirect-agent.hourly-health`` — fires every 3600 s,
  runs ``yadirect-agent health --days=1 --json``.

Why ``yadirect-agent health`` and not ``yadirect-agent run``:
the latter requires a task description argument and is built for
human-driven invocation. ``health`` is read-only, exits with code
0 / 1 (suitable for cron alerting), and produces JSON suitable
for log aggregation. When autonomous-mode runs land in Phase 3,
operators re-run ``schedule install`` to update both plists; the
slice 1 contract evolves cleanly.

Why ``launchctl load -w`` instead of ``launchctl bootstrap``:
``bootstrap`` (the Big Sur+ idiom) requires a uid-bound domain
identifier (``gui/$UID``) and a separately-loaded ``enable``
call. ``load -w`` works on every macOS version from El Capitan
forward and writes the job to launchd's override database in
one call, which means a clean ``unload`` at remove-time clears
state.

All ``launchctl`` invocations go through ``run_launchctl`` so
tests can replace it with an in-memory spy. Production
implementation is one ``subprocess.run`` call.

Atomicity: each plist is written via tempfile + ``os.replace``
in the same directory as the target. A partial-write crash
leaves the original plist (or no file) — never a half-written
XML that ``launchd`` would reject as malformed.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import structlog

_log = structlog.get_logger(component="services.scheduler.macos")

DAILY_LABEL = "com.yadirect-agent.daily"
HOURLY_LABEL = "com.yadirect-agent.hourly-health"


def run_launchctl(args: list[str]) -> None:
    """Invoke ``launchctl`` with ``args``. Indirection point for tests.

    Production: ``subprocess.run(["launchctl", *args], check=True)``
    fires the call. Tests monkeypatch this symbol with an in-memory
    spy so no real subprocess runs.

    A non-zero exit propagates as ``CalledProcessError``. Operators
    see the ``launchctl`` stderr in the audit log via ``capture_output``;
    we do NOT swallow failures — a failed ``load`` means the job is
    not actually scheduled, and pretending success would be a worse
    failure mode than a loud raise.
    """
    # ``launchctl`` is at /bin/launchctl on every supported macOS;
    # the absolute path defeats the bandit S607 partial-path warning.
    subprocess.run(  # noqa: S603 — args constructed in this module, no shell
        ["/bin/launchctl", *args],
        check=True,
        capture_output=True,
    )


def _validate_executable(executable: str) -> None:
    if not Path(executable).is_absolute():
        msg = (
            "executable must be an absolute path; launchd resolves "
            f"ProgramArguments[0] relative to its own CWD (/), got {executable!r}"
        )
        raise ValueError(msg)


def generate_daily_plist(*, executable: str, log_dir: Path) -> bytes:
    """Daily 08:00 health plist (08:00 local, runs 7-day check).

    Returns the XML bytes ``launchd`` will load. Plist contents:

    - ``Label``: ``com.yadirect-agent.daily`` (pinned).
    - ``ProgramArguments``:
      ``[executable, "health", "--days=7", "--json"]``.
    - ``StartCalendarInterval``: ``{Hour: 8, Minute: 0}``.
    - ``StandardOutPath`` / ``StandardErrorPath``: ``log_dir / daily.log``
      / ``daily.err``.
    """
    _validate_executable(executable)
    plist_dict = {
        "Label": DAILY_LABEL,
        "ProgramArguments": [executable, "health", "--days=7", "--json"],
        "RunAtLoad": False,
        "StandardOutPath": str(log_dir / "daily.log"),
        "StandardErrorPath": str(log_dir / "daily.err"),
        "StartCalendarInterval": {"Hour": 8, "Minute": 0},
    }
    return plistlib.dumps(plist_dict)


def generate_hourly_plist(*, executable: str, log_dir: Path) -> bytes:
    """Hourly health plist (every 3600 s, today's 1-day window).

    Same shape as the daily plist except:
    - ``Label``: ``com.yadirect-agent.hourly-health``.
    - ``--days=1`` instead of ``--days=7``.
    - ``StartInterval=3600`` instead of ``StartCalendarInterval``.

    Hourly's narrower window reflects what the operator actually
    wants from hour-to-hour observability — yesterday's signals
    have already been surfaced by the daily; the hourly catches
    today's rapidly-evolving issues (rejected ad just published,
    a campaign that started burning at 14:00).
    """
    _validate_executable(executable)
    plist_dict = {
        "Label": HOURLY_LABEL,
        "ProgramArguments": [executable, "health", "--days=1", "--json"],
        "RunAtLoad": False,
        "StandardOutPath": str(log_dir / "hourly.log"),
        "StandardErrorPath": str(log_dir / "hourly.err"),
        "StartInterval": 3600,
    }
    return plistlib.dumps(plist_dict)


@dataclass(frozen=True)
class PlistInstallResult:
    """What ``MacOSScheduler.install`` returns to the CLI.

    The CLI surfaces these paths to the operator so they can
    ``tail -f`` the logs or ``launchctl unload`` manually if
    something goes wrong.
    """

    daily_plist_path: Path
    hourly_plist_path: Path
    log_dir: Path


@dataclass(frozen=True)
class ScheduleStatus:
    """What ``MacOSScheduler.status`` returns.

    ``installed=True`` requires BOTH plists present. A
    half-installed state (one missing) reads as
    ``installed=False`` so the operator's ``schedule status``
    after a partial / failed install doesn't lie.
    """

    installed: bool
    daily_plist_path: Path
    hourly_plist_path: Path


class MacOSScheduler:
    """Lifecycle for the daily + hourly LaunchAgents.

    Constructed with explicit paths (``launchagents_dir``,
    ``log_dir``) so tests don't depend on the real home directory
    and the CLI passes ``Path.home() / 'Library' / 'LaunchAgents'``
    in production.
    """

    def __init__(
        self,
        *,
        launchagents_dir: Path,
        log_dir: Path,
        executable: str,
    ) -> None:
        _validate_executable(executable)
        self._launchagents_dir = launchagents_dir
        self._log_dir = log_dir
        self._executable = executable

    @property
    def daily_plist_path(self) -> Path:
        return self._launchagents_dir / f"{DAILY_LABEL}.plist"

    @property
    def hourly_plist_path(self) -> Path:
        return self._launchagents_dir / f"{HOURLY_LABEL}.plist"

    # -- Writes ----------------------------------------------------------

    def install(self) -> PlistInstallResult:
        """Write both plists atomically and ``launchctl load -w`` them."""
        self._launchagents_dir.mkdir(parents=True, exist_ok=True)
        self._log_dir.mkdir(parents=True, exist_ok=True)

        daily = generate_daily_plist(executable=self._executable, log_dir=self._log_dir)
        hourly = generate_hourly_plist(executable=self._executable, log_dir=self._log_dir)

        self._atomic_write(self.daily_plist_path, daily)
        self._atomic_write(self.hourly_plist_path, hourly)

        run_launchctl(["load", "-w", str(self.daily_plist_path)])
        run_launchctl(["load", "-w", str(self.hourly_plist_path)])

        _log.info(
            "scheduler.macos.installed",
            daily=str(self.daily_plist_path),
            hourly=str(self.hourly_plist_path),
        )
        return PlistInstallResult(
            daily_plist_path=self.daily_plist_path,
            hourly_plist_path=self.hourly_plist_path,
            log_dir=self._log_dir,
        )

    def remove(self) -> None:
        """Idempotent removal: unload + delete each plist if present.

        Same shape as ``KeyringTokenStore.delete`` /
        ``BusinessProfileStore.delete``: a no-op on a fresh
        account is the success path, not an error.
        """
        for path in (self.daily_plist_path, self.hourly_plist_path):
            if not path.exists():
                continue
            # ``unload`` matched with the original ``load -w`` call.
            try:
                run_launchctl(["unload", str(path)])
            except subprocess.CalledProcessError:
                # The job may already be unloaded (operator manually
                # ran ``launchctl unload`` between install and our
                # remove). Continue to delete the file so the on-disk
                # state matches the operator's intent.
                _log.warning(
                    "scheduler.macos.unload_failed",
                    path=str(path),
                    note="continuing with file delete",
                )
            path.unlink(missing_ok=True)
        _log.info(
            "scheduler.macos.removed",
            launchagents_dir=str(self._launchagents_dir),
        )

    # -- Reads -----------------------------------------------------------

    def status(self) -> ScheduleStatus:
        """Read-only state probe: both plists present?"""
        both_present = self.daily_plist_path.exists() and self.hourly_plist_path.exists()
        return ScheduleStatus(
            installed=both_present,
            daily_plist_path=self.daily_plist_path,
            hourly_plist_path=self.hourly_plist_path,
        )

    # -- Internals -------------------------------------------------------

    @staticmethod
    def _atomic_write(target: Path, payload: bytes) -> None:
        """Same atomicity contract as ``BusinessProfileStore.save``.

        Tempfile sits in the same directory as the target so
        ``os.replace`` is a same-filesystem atomic rename. Failed
        rename cleans the tempfile and re-raises so callers know
        the write did not happen.
        """
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            tmp.write(payload)
            tmp_path = Path(tmp.name)
        try:
            os.replace(tmp_path, target)
        except OSError:
            tmp_path.unlink(missing_ok=True)
            raise


__all__ = [
    "DAILY_LABEL",
    "HOURLY_LABEL",
    "MacOSScheduler",
    "PlistInstallResult",
    "ScheduleStatus",
    "generate_daily_plist",
    "generate_hourly_plist",
    "run_launchctl",
]
