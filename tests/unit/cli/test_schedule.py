"""Tests for ``yadirect-agent schedule ...`` CLI subapp (M15.6 slices 1+2).

Three commands operators run by hand:

- ``schedule install [--platform=auto/macos/linux/windows]``
  — generate plists or systemd unit files, write them to the
  platform's standard location, call ``launchctl load -w`` or
  ``systemctl --user enable --now``. macOS + Linux ship in
  slices 1+2; Windows still prints a "shipping in slice 3"
  message and exits 2.
- ``schedule status`` — read the on-disk plists / unit files,
  report installed / not installed (paths included so the
  operator can tail the logs).
- ``schedule remove`` — call ``launchctl unload`` + delete the
  plist files (macOS), or ``systemctl --user disable --now`` +
  delete the unit files (Linux). Idempotent on fresh accounts.

Tests patch ``MacOSScheduler`` / ``LinuxScheduler`` with in-memory
spies; the actual plist / unit-file generation + subprocess
behaviour is covered in
``tests/unit/services/scheduler/test_macos.py`` and
``test_linux.py``.

Exit-code conventions:
- 0 — success (install completed, status read, remove completed).
- 2 — platform not yet supported (Windows until slice 3) OR
  install failure (subprocess error from launchctl / systemctl).
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

    def test_install_windows_prints_not_yet_supported_and_exits_2(
        self,
        runner: CliRunner,
    ) -> None:
        result = runner.invoke(app, ["schedule", "install", "--platform=windows"])

        assert result.exit_code == 2, result.output
        assert "windows" in result.output.lower()

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

        # Status is a read; "not installed" is a valid state, not
        # an error. Exit 0 lets cron-like wrappers tell normal
        # state from invocation failure.
        assert result.exit_code == 0, result.output
        assert "not installed" in result.stdout.lower() or "not yet" in result.stdout.lower()

    def test_status_linux_reports_installed(
        self,
        runner: CliRunner,
        fake_linux_scheduler: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Slice 2 wires the linux dispatch — status prints the four
        # unit-file paths so operators can ``systemctl --user
        # status yadirect-agent-daily.timer`` or tail the log dir.
        monkeypatch.setattr("sys.platform", "linux")

        result = runner.invoke(app, ["schedule", "status"])

        assert result.exit_code == 0, result.output
        assert "installed" in result.stdout.lower()
        # Both timers' presence is part of the operator-visible
        # output. Pinning these strings prevents a regression that
        # silently dropped a path from the rendered status.
        assert "yadirect-agent-daily.timer" in result.stdout
        assert "yadirect-agent-hourly.timer" in result.stdout

    def test_status_linux_reports_not_installed(
        self,
        runner: CliRunner,
        fake_linux_scheduler: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Same exit-0 contract as macOS: "not installed" is a
        # valid state, not an error. Keeps cron-like wrappers
        # honest about telling normal state apart from invocation
        # failure.
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
        assert "not installed" in result.stdout.lower()


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
