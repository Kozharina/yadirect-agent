"""Tests for ``yadirect-agent schedule ...`` CLI subapp (M15.6 slices 1+2+3).

Three commands operators run by hand:

- ``schedule install [--platform=auto/macos/linux/windows]``
  — generate plists, systemd unit files, or Task Scheduler XML,
  write them to the platform's standard location, then call
  ``launchctl load -w`` (macOS), ``systemctl --user enable
  --now`` (Linux), or ``schtasks /Create /XML /F`` (Windows).
  All three platforms ship; auto-detection picks the right
  branch from ``sys.platform``.
- ``schedule status`` — read the on-disk plists / unit files /
  XML, report installed / not installed (paths included so the
  operator can tail the logs).
- ``schedule remove`` — call ``launchctl unload`` + delete the
  plist files (macOS), ``systemctl --user disable --now`` +
  delete unit files (Linux), or ``schtasks /Delete`` + delete
  XML files (Windows). Idempotent on fresh accounts.

Tests patch ``MacOSScheduler`` / ``LinuxScheduler`` /
``WindowsScheduler`` with in-memory spies; the actual plist /
unit-file / XML generation + subprocess behaviour is covered in
``tests/unit/services/scheduler/test_macos.py``,
``test_linux.py``, and ``test_windows.py``.

Exit-code conventions:
- 0 — success (install completed, status read, remove completed).
- 2 — unsupported platform (cygwin, aix, etc.) OR install
  failure (subprocess error from launchctl / systemctl /
  schtasks).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from yadirect_agent.cli.main import app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def fake_scheduler(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> MagicMock:
    """Replace ``MacOSScheduler`` with a MagicMock so CLI tests
    don't depend on real launchctl.

    The mock returns realistic ``PlistInstallResult`` /
    ``ScheduleStatus`` instances so the rendering layer (which
    reads attributes off them) gets the right shape.
    """
    from yadirect_agent.services.scheduler.macos import (
        PlistInstallResult,
        ScheduleStatus,
    )

    instance = MagicMock()
    instance.install.return_value = PlistInstallResult(
        daily_plist_path=tmp_path / "daily.plist",
        hourly_plist_path=tmp_path / "hourly.plist",
        log_dir=tmp_path / "logs",
    )
    instance.status.return_value = ScheduleStatus(
        installed=True,
        daily_plist_path=tmp_path / "daily.plist",
        hourly_plist_path=tmp_path / "hourly.plist",
    )
    instance.remove.return_value = None

    cls = MagicMock(return_value=instance)
    monkeypatch.setattr("yadirect_agent.cli.main.MacOSScheduler", cls)
    return instance


@pytest.fixture
def fake_linux_scheduler(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> MagicMock:
    """Replace ``LinuxScheduler`` with a MagicMock so CLI tests
    don't depend on real systemctl.

    Mirrors ``fake_scheduler`` for the macOS path; returns
    realistic ``LinuxInstallResult`` / ``ScheduleStatus`` instances
    so the rendering layer (which reads attributes off them) gets
    the right shape.
    """
    from yadirect_agent.services.scheduler.linux import (
        LinuxInstallResult,
        ScheduleStatus,
    )

    instance = MagicMock()
    instance.install.return_value = LinuxInstallResult(
        daily_service_path=tmp_path / "yadirect-agent-daily.service",
        daily_timer_path=tmp_path / "yadirect-agent-daily.timer",
        hourly_service_path=tmp_path / "yadirect-agent-hourly.service",
        hourly_timer_path=tmp_path / "yadirect-agent-hourly.timer",
        log_dir=tmp_path / "logs",
    )
    instance.status.return_value = ScheduleStatus(
        installed=True,
        daily_service_path=tmp_path / "yadirect-agent-daily.service",
        daily_timer_path=tmp_path / "yadirect-agent-daily.timer",
        hourly_service_path=tmp_path / "yadirect-agent-hourly.service",
        hourly_timer_path=tmp_path / "yadirect-agent-hourly.timer",
    )
    instance.remove.return_value = None

    cls = MagicMock(return_value=instance)
    monkeypatch.setattr("yadirect_agent.cli.main.LinuxScheduler", cls)
    return instance


@pytest.fixture
def fake_windows_scheduler(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> MagicMock:
    """Replace ``WindowsScheduler`` with a MagicMock so CLI tests
    don't depend on real schtasks.

    Mirrors ``fake_scheduler`` (macOS) / ``fake_linux_scheduler``
    (Linux); returns realistic ``WindowsInstallResult`` /
    ``ScheduleStatus`` instances so the rendering layer (which
    reads attributes off them) gets the right shape.
    """
    from yadirect_agent.services.scheduler.windows import (
        ScheduleStatus,
        WindowsInstallResult,
    )

    instance = MagicMock()
    instance.install.return_value = WindowsInstallResult(
        daily_xml_path=tmp_path / "yadirect-agent-daily.xml",
        hourly_xml_path=tmp_path / "yadirect-agent-hourly.xml",
        log_dir=tmp_path / "logs",
    )
    instance.status.return_value = ScheduleStatus(
        installed=True,
        daily_xml_path=tmp_path / "yadirect-agent-daily.xml",
        hourly_xml_path=tmp_path / "yadirect-agent-hourly.xml",
    )
    instance.remove.return_value = None

    cls = MagicMock(return_value=instance)
    monkeypatch.setattr("yadirect_agent.cli.main.WindowsScheduler", cls)
    return instance


class TestScheduleInstall:
    def test_install_macos_explicit_succeeds(
        self,
        runner: CliRunner,
        fake_scheduler: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # ``--platform=macos`` skips auto-detection; use this in CI
        # / Docker where ``sys.platform`` may not match the target
        # operator's machine.
        monkeypatch.setattr("shutil.which", lambda _: "/usr/local/bin/yadirect-agent")

        result = runner.invoke(app, ["schedule", "install", "--platform=macos"])

        assert result.exit_code == 0, result.output
        # Operator-visible summary mentions the daily + hourly plists.
        assert "daily" in result.stdout.lower()
        assert "hourly" in result.stdout.lower()
        # The fake scheduler's install was actually called.
        fake_scheduler.install.assert_called_once()

    def test_install_auto_uses_macos_on_darwin(
        self,
        runner: CliRunner,
        fake_scheduler: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("sys.platform", "darwin")
        monkeypatch.setattr("shutil.which", lambda _: "/usr/local/bin/yadirect-agent")

        result = runner.invoke(app, ["schedule", "install"])  # default --platform=auto

        assert result.exit_code == 0, result.output
        fake_scheduler.install.assert_called_once()

    def test_install_linux_explicit_succeeds(
        self,
        runner: CliRunner,
        fake_linux_scheduler: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Slice 2 ships systemd ``--user`` timers. Operators on
        # Linux now get the same install / status / remove surface
        # as macOS — the platform branch is no longer a stub.
        monkeypatch.setattr("shutil.which", lambda _: "/usr/local/bin/yadirect-agent")

        result = runner.invoke(app, ["schedule", "install", "--platform=linux"])

        assert result.exit_code == 0, result.output
        # Operator-visible summary mentions both timers + the log
        # dir, mirroring the macOS install summary.
        assert "daily" in result.stdout.lower()
        assert "hourly" in result.stdout.lower()
        # The fake LinuxScheduler's install was actually called.
        fake_linux_scheduler.install.assert_called_once()

    def test_install_auto_uses_linux_on_linux_platform(
        self,
        runner: CliRunner,
        fake_linux_scheduler: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # ``sys.platform == "linux"`` (or any "linux*" variant)
        # auto-resolves to the systemd dispatch — operators don't
        # need to pass ``--platform`` on a normal Linux install.
        monkeypatch.setattr("sys.platform", "linux")
        monkeypatch.setattr("shutil.which", lambda _: "/usr/local/bin/yadirect-agent")

        result = runner.invoke(app, ["schedule", "install"])

        assert result.exit_code == 0, result.output
        fake_linux_scheduler.install.assert_called_once()

    def test_install_windows_explicit_succeeds(
        self,
        runner: CliRunner,
        fake_windows_scheduler: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Slice 3 ships Task Scheduler. Operators on Windows now
        # get the same install / status / remove surface as
        # macOS + Linux — the platform branch is no longer a
        # stub. ``--platform=windows`` skips auto-detection so
        # the test runs the right branch on a non-Windows CI box.
        monkeypatch.setattr("shutil.which", lambda _: r"C:\opt\yadirect-agent.exe")

        result = runner.invoke(app, ["schedule", "install", "--platform=windows"])

        assert result.exit_code == 0, result.output
        # Operator-visible summary mentions both XML files + the
        # log dir, mirroring the macOS / Linux install summaries.
        assert "daily" in result.stdout.lower()
        assert "hourly" in result.stdout.lower()
        # The fake WindowsScheduler's install was actually called.
        fake_windows_scheduler.install.assert_called_once()

    def test_install_auto_uses_windows_on_win32(
        self,
        runner: CliRunner,
        fake_windows_scheduler: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # ``sys.platform == "win32"`` auto-resolves to the Task
        # Scheduler dispatch — operators on Windows don't need
        # to pass ``--platform`` on a normal install.
        monkeypatch.setattr("sys.platform", "win32")
        monkeypatch.setattr("shutil.which", lambda _: r"C:\opt\yadirect-agent.exe")

        result = runner.invoke(app, ["schedule", "install"])

        assert result.exit_code == 0, result.output
        fake_windows_scheduler.install.assert_called_once()

    def test_install_auto_unknown_platform_exits_2(
        self,
        runner: CliRunner,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # ``sys.platform`` set to something none of the slices know
        # (e.g. ``cygwin``, ``aix``, future platforms). Refuse with
        # a clear message rather than guess.
        monkeypatch.setattr("sys.platform", "aix")

        result = runner.invoke(app, ["schedule", "install"])

        assert result.exit_code == 2, result.output
        assert "aix" in result.output.lower() or "unsupported" in result.output.lower()

    def test_install_executable_not_found_exits_2(
        self,
        runner: CliRunner,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # ``shutil.which`` returning ``None`` means
        # ``yadirect-agent`` isn't on PATH. The operator probably
        # ran ``schedule install`` from a venv that's not active —
        # tell them so they can act.
        monkeypatch.setattr("sys.platform", "darwin")
        monkeypatch.setattr("shutil.which", lambda _: None)

        result = runner.invoke(app, ["schedule", "install", "--platform=macos"])

        assert result.exit_code == 2, result.output
        assert "executable" in result.output.lower() or "path" in result.output.lower()


class TestScheduleStatus:
    def test_status_reports_installed(
        self,
        runner: CliRunner,
        fake_scheduler: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("sys.platform", "darwin")

        result = runner.invoke(app, ["schedule", "status"])

        assert result.exit_code == 0, result.output
        # Operator sees installed=True and the plist paths so they
        # can ``tail -f`` the logs without grepping our source.
        assert "installed" in result.stdout.lower()

    def test_status_reports_not_installed(
        self,
        runner: CliRunner,
        fake_scheduler: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from yadirect_agent.services.scheduler.macos import ScheduleStatus

        monkeypatch.setattr("sys.platform", "darwin")
        fake_scheduler.status.return_value = ScheduleStatus(
            installed=False,
            daily_plist_path=Path("/tmp/daily.plist"),
            hourly_plist_path=Path("/tmp/hourly.plist"),
        )

        result = runner.invoke(app, ["schedule", "status"])

        # Status is a read; the "not configured" state is valid,
        # not an error. Exit 0 lets cron-like wrappers tell normal
        # state from invocation failure. Operator-facing CLI text
        # lives in Russian per CLAUDE.md `<language_conventions>`
        # — Anna (target persona) reads it on her terminal.
        assert result.exit_code == 0, result.output
        assert "не настроено" in result.stdout.lower()

    def test_status_linux_reports_installed(
        self,
        runner: CliRunner,
        fake_linux_scheduler: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Slice 2 wires the linux dispatch — status prints both
        # unit pairs so the operator can ``systemctl --user
        # status yadirect-agent-daily.timer`` or tail the log dir.
        # We deliberately don't pin the full ``yadirect-agent-*.timer``
        # filenames here: rich.Console wraps long lines at the
        # terminal width and on CI runners with deep tmpdirs the
        # path overflow splits the filename across a newline,
        # causing a brittle substring test. Short tokens (daily /
        # hourly) survive wrap and are still a sufficient regression
        # signal — if the dispatch silently dropped both pairs from
        # the rendered status, those tokens would be gone too.
        monkeypatch.setattr("sys.platform", "linux")

        result = runner.invoke(app, ["schedule", "status"])

        assert result.exit_code == 0, result.output
        assert "installed" in result.stdout.lower()
        assert "daily" in result.stdout.lower()
        assert "hourly" in result.stdout.lower()
        # The fake LinuxScheduler.status was actually called — pins
        # that the dispatch routes status through the linux path,
        # not silently through the macos one.
        fake_linux_scheduler.status.assert_called_once()

    def test_status_linux_reports_not_installed(
        self,
        runner: CliRunner,
        fake_linux_scheduler: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Same exit-0 contract as macOS: the "not configured" state
        # is valid, not an error. Operator-facing text in Russian
        # per CLAUDE.md `<language_conventions>`.
        from yadirect_agent.services.scheduler.linux import ScheduleStatus

        monkeypatch.setattr("sys.platform", "linux")
        fake_linux_scheduler.status.return_value = ScheduleStatus(
            installed=False,
            daily_service_path=Path("/tmp/d.service"),
            daily_timer_path=Path("/tmp/d.timer"),
            hourly_service_path=Path("/tmp/h.service"),
            hourly_timer_path=Path("/tmp/h.timer"),
        )

        result = runner.invoke(app, ["schedule", "status"])
        assert result.exit_code == 0, result.output
        assert "не настроено" in result.stdout.lower()

    def test_status_windows_reports_installed(
        self,
        runner: CliRunner,
        fake_windows_scheduler: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Slice 3 wires the windows dispatch — status prints
        # both XML pairs so the operator can ``schtasks /Query
        # /TN yadirect-agent-daily`` or tail the log dir.
        # Same line-wrap caveat as the linux test: rich.Console
        # may wrap long paths on narrow CI terminals, so we pin
        # short tokens (daily / hourly) rather than full filenames.
        monkeypatch.setattr("sys.platform", "win32")

        result = runner.invoke(app, ["schedule", "status"])

        assert result.exit_code == 0, result.output
        assert "installed" in result.stdout.lower()
        assert "daily" in result.stdout.lower()
        assert "hourly" in result.stdout.lower()
        # The fake WindowsScheduler.status was actually called —
        # pins that the dispatch routes status through the
        # windows path, not silently through macos / linux.
        fake_windows_scheduler.status.assert_called_once()

    def test_status_windows_reports_not_installed(
        self,
        runner: CliRunner,
        fake_windows_scheduler: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Same exit-0 contract as macOS / Linux: the "not configured"
        # state is valid. Operator-facing text in Russian per
        # CLAUDE.md `<language_conventions>`.
        from yadirect_agent.services.scheduler.windows import ScheduleStatus

        monkeypatch.setattr("sys.platform", "win32")
        fake_windows_scheduler.status.return_value = ScheduleStatus(
            installed=False,
            daily_xml_path=Path(r"C:\fake\daily.xml"),
            hourly_xml_path=Path(r"C:\fake\hourly.xml"),
        )

        result = runner.invoke(app, ["schedule", "status"])
        assert result.exit_code == 0, result.output
        assert "не настроено" in result.stdout.lower()


class TestScheduleRemove:
    def test_remove_succeeds(
        self,
        runner: CliRunner,
        fake_scheduler: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("sys.platform", "darwin")

        result = runner.invoke(app, ["schedule", "remove"])

        assert result.exit_code == 0, result.output
        fake_scheduler.remove.assert_called_once()

    def test_remove_idempotent_on_fresh_account(
        self,
        runner: CliRunner,
        fake_scheduler: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # ``MacOSScheduler.remove`` is idempotent (verified at the
        # service layer); the CLI must surface that as exit 0,
        # not as an error.
        monkeypatch.setattr("sys.platform", "darwin")
        fake_scheduler.remove.return_value = None

        result = runner.invoke(app, ["schedule", "remove"])
        assert result.exit_code == 0

    def test_remove_linux_succeeds(
        self,
        runner: CliRunner,
        fake_linux_scheduler: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Parity with the macOS remove: slice 2 routes ``remove``
        # to ``LinuxScheduler.remove`` which itself is idempotent
        # at the service layer. The CLI returns exit 0.
        monkeypatch.setattr("sys.platform", "linux")

        result = runner.invoke(app, ["schedule", "remove"])

        assert result.exit_code == 0, result.output
        fake_linux_scheduler.remove.assert_called_once()

    def test_remove_windows_succeeds(
        self,
        runner: CliRunner,
        fake_windows_scheduler: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Parity with macOS / Linux remove: slice 3 routes
        # ``remove`` to ``WindowsScheduler.remove`` which itself
        # is idempotent at the service layer. The CLI returns
        # exit 0 even on a fresh account (``schtasks /Delete``
        # returns non-zero when the task doesn't exist; the
        # service tolerates that and proceeds to file cleanup).
        monkeypatch.setattr("sys.platform", "win32")

        result = runner.invoke(app, ["schedule", "remove"])

        assert result.exit_code == 0, result.output
        fake_windows_scheduler.remove.assert_called_once()
