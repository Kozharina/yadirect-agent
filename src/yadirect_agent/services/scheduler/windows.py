"""Windows Task Scheduler scheduler (M15.6 slice 3).

Two scheduled jobs, mirroring slices 1 (macOS LaunchAgent) and 2
(Linux systemd ``--user`` timers):

- ``yadirect-agent-daily`` fires daily at 08:00 local time and runs
  ``yadirect-agent health --days=7 --json``.
- ``yadirect-agent-hourly`` fires hourly (``Repetition`` = ``PT1H``)
  starting from a fixed past ``StartBoundary`` and runs
  ``yadirect-agent health --days=1 --json``.

Why Task Scheduler XML 1.4 and not the simpler ``schtasks /Create``
flag-based form: only the XML payload exposes the full surface
operators expect (``MultipleInstancesPolicy``,
``StartWhenAvailable``, ``DisallowStartIfOnBatteries``,
``Repetition`` for sub-daily cadences). The flag form has to fall
back to default settings, several of which are wrong for a health
check (e.g. ``DontStartOnBatteries=true`` is the default and would
silently skip runs on a laptop). The XML form lets us pin the
right defaults explicitly.

Why ``cmd.exe /c "..."`` wraps the agent invocation: Task Scheduler
has no ``StandardOutput=append:...`` analogue for systemd or
``StandardOutPath`` for launchd. The portable workaround is
``cmd.exe /c "yadirect-agent.exe health ... >> daily.log 2>>
daily.err"``: cmd interprets ``>>`` and ``2>>`` as append redirects
*after* Task Scheduler hands it the full argument string. The cost
is one extra process per run (cmd.exe is ~5ms cold), well below
the noise floor of the health check itself.

Why ``StartWhenAvailable=true``: the analogue of slice 2's
``Persistent=true`` and slice 1's implicit launchd catch-up
behaviour. A laptop that was asleep at 08:00 fires the missed
daily run on next wake instead of silently dropping it. The
hourly task doesn't strictly need it (the next ``PT1H`` window is
at most an hour away), but we set it on both for consistency —
operators can't reason about a setting that's "true here but
false there".

Why ``MultipleInstancesPolicy=IgnoreNew``: if a previous run is
still going when the next trigger fires, drop the new run. Health
checks are short; overlap is a smell that points at a stuck
process or an unbounded loop. Better to skip than to pile up.

Why UTF-16 LE with BOM for the on-disk XML: Microsoft's Task
Scheduler GUI exporter emits XML in this format, and ``schtasks
/Create /XML`` accepts it on every Windows version we support.
UTF-8 also works on Windows 10/11 but UTF-16 is the canonical
interchange format that survives every legacy schtasks version.

All ``schtasks`` invocations go through ``run_schtasks`` so tests
can replace it with an in-memory spy. Production implementation
is a single ``subprocess.run`` call. The hard-coded
``C:\\Windows\\System32\\schtasks.exe`` path matches every
standard Windows install (Windows installed to non-standard
drives override ``run_schtasks`` directly — same escape hatch
slice 1 documents for ``run_launchctl`` and slice 2 for
``run_systemctl``).

Atomicity: each XML file is written via tempfile + ``os.replace``
in the same directory as the target. A partial-write crash leaves
the original (or no file) — never a half-written XML that
``schtasks /Create /XML`` would parse-error on.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

import structlog

_log = structlog.get_logger(component="services.scheduler.windows")

DAILY_TASK_NAME = "yadirect-agent-daily"
HOURLY_TASK_NAME = "yadirect-agent-hourly"

# Task Scheduler XML schema 1.4 — Windows Vista+. Every supported
# Windows version (7/8/10/11) parses this version cleanly.
TASK_NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"

# ``C:\Windows\System32\schtasks.exe`` is the canonical location on
# every standard Windows install. Absolute path defeats the bandit
# S607 partial-path warning. Operators on non-standard installs
# (Windows on D:, custom %SystemRoot%) override ``run_schtasks``
# directly — same escape hatch slice 1 / slice 2 offer for
# launchctl / systemctl.
_SCHTASKS_PATH = r"C:\Windows\System32\schtasks.exe"

# ``cmd.exe`` is the action executable; the agent runs inside its
# argument string so ``>>`` redirects work. Same locality reason
# as schtasks.exe.
_CMD_EXE_PATH = r"C:\Windows\System32\cmd.exe"

# Fixed past ``StartBoundary`` so the generated XML is deterministic
# across re-installs. Task Scheduler's contract: trigger fires at
# the next ``StartBoundary`` time-of-day after now (calendar) or
# every ``Repetition.Interval`` from ``StartBoundary`` (time). A
# date in 2024 is far enough in the past that the next valid fire
# is always within a day of install, regardless of when install
# runs.
_FIXED_START_BOUNDARY = "2024-01-01T08:00:00"


def run_schtasks(args: list[str]) -> None:
    """Invoke ``schtasks.exe`` with ``args``. Indirection point for tests.

    Production: ``subprocess.run([SCHTASKS_PATH, *args], check=True,
    capture_output=True)``. Tests monkeypatch this symbol with an
    in-memory spy so no real subprocess runs (and so the test
    suite works on macOS / Linux dev boxes where schtasks doesn't
    exist).

    A non-zero exit propagates as ``CalledProcessError``.
    ``capture_output`` surfaces ``schtasks`` stderr to the operator
    via the audit log; swallowing failures would mean the task is
    not actually scheduled and pretending success would be a worse
    failure mode than a loud raise.
    """
    subprocess.run(  # noqa: S603 — args constructed in this module, no shell
        [_SCHTASKS_PATH, *args],
        check=True,
        capture_output=True,
    )


def _validate_executable(executable: str) -> None:
    # ``PureWindowsPath`` (vs the platform-native ``Path``) so the
    # validator gives consistent answers on macOS / Linux dev boxes
    # AND on Windows production: ``PureWindowsPath("C:\\...")`` is
    # absolute everywhere, ``PureWindowsPath("yadirect-agent.exe")``
    # is relative everywhere. Without this, the test suite running
    # on macOS sees ``Path("C:\\opt\\...").is_absolute() == False``
    # (PosixPath doesn't know about drive letters) and rejects valid
    # production-Windows inputs.
    if not PureWindowsPath(executable).is_absolute():
        msg = (
            "executable must be an absolute path; cmd.exe inside Task "
            "Scheduler resolves a relative value against C:\\Windows\\System32 "
            f"(its CWD when launched by Task Scheduler), got {executable!r}"
        )
        raise ValueError(msg)


def _to_utf16_le_with_bom(xml_str: str) -> bytes:
    """Encode XML as UTF-16 LE with BOM (the format schtasks expects).

    Python's ``"utf-16"`` encoding picks endianness from the
    platform's native byte order and prepends a matching BOM —
    portable, but on a big-endian host it would emit UTF-16 BE,
    which schtasks does not accept. Force little-endian explicitly
    by writing the BOM ourselves and using ``utf-16-le`` (which
    does not auto-prepend a BOM).
    """
    return b"\xff\xfe" + xml_str.encode("utf-16-le")


def _calendar_trigger_at_eight() -> ET.Element:
    """Daily 08:00 trigger element (calendar-based, every day).

    ``ScheduleByDay`` + ``DaysInterval=1`` is the Task Scheduler
    idiom for "every day at the same wall-clock time". The wall-
    clock time comes from ``StartBoundary``'s time component
    (08:00:00).
    """
    trigger = ET.Element("CalendarTrigger")
    ET.SubElement(trigger, "StartBoundary").text = _FIXED_START_BOUNDARY
    ET.SubElement(trigger, "Enabled").text = "true"
    schedule = ET.SubElement(trigger, "ScheduleByDay")
    ET.SubElement(schedule, "DaysInterval").text = "1"
    return trigger


def _time_trigger_hourly() -> ET.Element:
    """Hourly trigger element (time-based, ``PT1H`` repetition).

    ``Repetition.Interval=PT1H`` (ISO 8601 one-hour duration) makes
    the trigger re-fire every hour from ``StartBoundary``
    indefinitely. ``StartWhenAvailable=true`` (in Settings) handles
    the missed-run-while-asleep case the same way slice 2's
    ``Persistent=true`` does.
    """
    trigger = ET.Element("TimeTrigger")
    ET.SubElement(trigger, "StartBoundary").text = _FIXED_START_BOUNDARY
    ET.SubElement(trigger, "Enabled").text = "true"
    rep = ET.SubElement(trigger, "Repetition")
    ET.SubElement(rep, "Interval").text = "PT1H"
    return trigger


def _build_task_xml(
    *,
    description: str,
    executable: str,
    days: int,
    log_dir: Path,
    log_basename: str,
    trigger: ET.Element,
) -> str:
    """Assemble a Task Scheduler 1.4 XML document.

    Common ``RegistrationInfo`` / ``Settings`` / ``Actions`` shape
    across daily and hourly; the only delta is the trigger element
    (``CalendarTrigger`` vs ``TimeTrigger``).
    """
    _validate_executable(executable)

    task = ET.Element(
        "Task",
        attrib={"version": "1.4", "xmlns": TASK_NAMESPACE},
    )

    reginfo = ET.SubElement(task, "RegistrationInfo")
    ET.SubElement(reginfo, "Description").text = description

    triggers_el = ET.SubElement(task, "Triggers")
    triggers_el.append(trigger)

    # See module docstring for why each Settings directive matters.
    settings = ET.SubElement(task, "Settings")
    ET.SubElement(settings, "MultipleInstancesPolicy").text = "IgnoreNew"
    ET.SubElement(settings, "DisallowStartIfOnBatteries").text = "false"
    ET.SubElement(settings, "StopIfGoingOnBatteries").text = "false"
    ET.SubElement(settings, "StartWhenAvailable").text = "true"
    ET.SubElement(settings, "Enabled").text = "true"

    actions = ET.SubElement(task, "Actions")
    exec_el = ET.SubElement(actions, "Exec")
    ET.SubElement(exec_el, "Command").text = _CMD_EXE_PATH
    log_path = log_dir / f"{log_basename}.log"
    err_path = log_dir / f"{log_basename}.err"
    # ``cmd.exe /c "<cmdline>"`` runs the quoted command and exits.
    # The redirects (``>>`` for append-stdout, ``2>>`` for append-
    # stderr) are interpreted by cmd, not by Task Scheduler. ET
    # auto-escapes ``>`` to ``&gt;`` on serialise; Task Scheduler
    # un-escapes back when handing the string to cmd.
    exec_el_args = f'/c "{executable} health --days={days} --json >> {log_path} 2>> {err_path}"'
    ET.SubElement(exec_el, "Arguments").text = exec_el_args

    ET.indent(task, space="  ")
    body = ET.tostring(task, encoding="unicode")
    # Microsoft's exporter emits the declaration with encoding
    # ``UTF-16``; the actual byte order is determined by the BOM.
    # ``schtasks /Create /XML`` requires the declaration to match
    # the on-disk encoding, so we pin both consistently.
    return f'<?xml version="1.0" encoding="UTF-16"?>\n{body}'


def generate_daily_xml(*, executable: str, log_dir: Path) -> str:
    """Daily 08:00 health task XML (runs ``health --days=7 --json``)."""
    return _build_task_xml(
        description="yadirect-agent daily health check (7-day window)",
        executable=executable,
        days=7,
        log_dir=log_dir,
        log_basename="daily",
        trigger=_calendar_trigger_at_eight(),
    )


def generate_hourly_xml(*, executable: str, log_dir: Path) -> str:
    """Hourly health task XML (runs ``health --days=1 --json``)."""
    return _build_task_xml(
        description="yadirect-agent hourly health check (today's window)",
        executable=executable,
        days=1,
        log_dir=log_dir,
        log_basename="hourly",
        trigger=_time_trigger_hourly(),
    )


@dataclass(frozen=True)
class WindowsInstallResult:
    """What ``WindowsScheduler.install`` returns to the CLI.

    The CLI surfaces these paths to the operator so they can
    ``Get-Content -Wait`` (PowerShell tail) the logs or
    ``schtasks /Query /TN <name>`` manually if something goes
    wrong.
    """

    daily_xml_path: Path
    hourly_xml_path: Path
    log_dir: Path


@dataclass(frozen=True)
class ScheduleStatus:
    """What ``WindowsScheduler.status`` returns.

    ``installed=True`` requires BOTH XML files present. Any
    partial state (one missing) reads as ``installed=False`` so
    the operator's ``schedule status`` after a partial / failed
    install doesn't lie. Same all-or-nothing contract as slices
    1 and 2.
    """

    installed: bool
    daily_xml_path: Path
    hourly_xml_path: Path


class WindowsScheduler:
    """Lifecycle for the daily + hourly Task Scheduler tasks.

    Constructed with explicit paths (``xml_dir``, ``log_dir``) so
    tests don't depend on the real home directory; the CLI passes
    ``%LOCALAPPDATA%\\yadirect-agent\\schedule`` and
    ``%LOCALAPPDATA%\\yadirect-agent\\logs`` in production.
    LocalAppData (non-roaming) is the right XDG-equivalent bucket
    for Task Scheduler XML and log files: tasks are per-machine
    in the Windows Task Store, so roaming AppData would create a
    drift between the on-disk XML and the registered tasks if the
    operator logs into a second machine.
    """

    def __init__(
        self,
        *,
        xml_dir: Path,
        log_dir: Path,
        executable: str,
    ) -> None:
        _validate_executable(executable)
        self._xml_dir = xml_dir
        self._log_dir = log_dir
        self._executable = executable

    @property
    def daily_xml_path(self) -> Path:
        return self._xml_dir / f"{DAILY_TASK_NAME}.xml"

    @property
    def hourly_xml_path(self) -> Path:
        return self._xml_dir / f"{HOURLY_TASK_NAME}.xml"

    def _all_xml_paths(self) -> tuple[Path, Path]:
        return (self.daily_xml_path, self.hourly_xml_path)

    # -- Writes ----------------------------------------------------------

    def install(self) -> WindowsInstallResult:
        """Write both XML files atomically and ``schtasks /Create /XML`` them.

        ``/F`` (force) on each Create makes re-install idempotent:
        if a task with the same name already exists, schtasks
        overwrites silently rather than prompting. Operators can
        change the executable path (e.g. after a venv move) and
        reinstall without a manual ``schedule remove`` first.

        Order: write XML to disk first, then call schtasks. A
        crash between the two leaves the XML on disk but the task
        unregistered — recoverable by re-running install.
        Calling schtasks before the XML exists would fail with a
        "file not found" error.
        """
        self._xml_dir.mkdir(parents=True, exist_ok=True)
        self._log_dir.mkdir(parents=True, exist_ok=True)

        self._atomic_write(
            self.daily_xml_path,
            _to_utf16_le_with_bom(
                generate_daily_xml(
                    executable=self._executable,
                    log_dir=self._log_dir,
                )
            ),
        )
        self._atomic_write(
            self.hourly_xml_path,
            _to_utf16_le_with_bom(
                generate_hourly_xml(
                    executable=self._executable,
                    log_dir=self._log_dir,
                )
            ),
        )

        run_schtasks(["/Create", "/TN", DAILY_TASK_NAME, "/XML", str(self.daily_xml_path), "/F"])
        run_schtasks(["/Create", "/TN", HOURLY_TASK_NAME, "/XML", str(self.hourly_xml_path), "/F"])

        _log.info(
            "scheduler.windows.installed",
            daily_xml=str(self.daily_xml_path),
            hourly_xml=str(self.hourly_xml_path),
        )
        return WindowsInstallResult(
            daily_xml_path=self.daily_xml_path,
            hourly_xml_path=self.hourly_xml_path,
            log_dir=self._log_dir,
        )

    def remove(self) -> None:
        """Idempotent removal: delete each task + delete each XML.

        ``schtasks /Delete /TN <name> /F`` removes the task from
        the Windows Task Store; the ``/F`` (force) bypasses the
        "are you sure?" prompt. ``CalledProcessError`` from
        ``/Delete`` is logged and ignored — the most likely cause
        is the operator already deleted the task manually via
        ``taskschd.msc`` or a prior ``schtasks /Delete``, and the
        on-disk XML cleanup must still proceed so their intent
        ("be gone") is honoured. Mirrors slice 1's ``launchctl
        unload`` failure tolerance and slice 2's ``systemctl
        disable`` failure tolerance.

        Same shape as ``KeyringTokenStore.delete``: a no-op on a
        fresh account is the success path, not an error.
        """
        for task_name in (DAILY_TASK_NAME, HOURLY_TASK_NAME):
            try:
                run_schtasks(["/Delete", "/TN", task_name, "/F"])
            except subprocess.CalledProcessError:
                _log.warning(
                    "scheduler.windows.delete_failed",
                    task=task_name,
                    note="continuing with file delete",
                )

        for path in self._all_xml_paths():
            path.unlink(missing_ok=True)

        _log.info("scheduler.windows.removed", xml_dir=str(self._xml_dir))

    # -- Reads -----------------------------------------------------------

    def status(self) -> ScheduleStatus:
        """Read-only state probe: both XML files present?

        Reads on-disk XML files rather than asking schtasks —
        same source-of-truth pattern as slices 1 and 2 (read
        plists / unit files, not launchctl / systemctl). Avoids
        a subprocess on the read path; tests don't have to mock
        one.
        """
        both_present = all(path.exists() for path in self._all_xml_paths())
        return ScheduleStatus(
            installed=both_present,
            daily_xml_path=self.daily_xml_path,
            hourly_xml_path=self.hourly_xml_path,
        )

    # -- Internals -------------------------------------------------------

    @staticmethod
    def _atomic_write(target: Path, payload: bytes) -> None:
        """Same atomicity contract as slice 1's ``MacOSScheduler._atomic_write``.

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
    "DAILY_TASK_NAME",
    "HOURLY_TASK_NAME",
    "TASK_NAMESPACE",
    "ScheduleStatus",
    "WindowsInstallResult",
    "WindowsScheduler",
    "generate_daily_xml",
    "generate_hourly_xml",
    "run_schtasks",
]
