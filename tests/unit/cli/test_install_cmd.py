"""Tests for ``install-into-claude-desktop`` / ``uninstall-from-claude-desktop``
CLI wiring (M15.2).

The pure logic is covered in ``test_install.py``; this file pins the
typer-layer concerns: argument parsing, exit codes, output formatting,
and the ``--config-path`` override that bypasses the OS-conditional
resolver (so tests don't have to monkeypatch platform globals every
time).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from yadirect_agent.cli.main import app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# --------------------------------------------------------------------------
# install-into-claude-desktop
# --------------------------------------------------------------------------


class TestInstallCmd:
    def test_explicit_config_path_creates_config(
        self,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        config_path = tmp_path / "claude_desktop_config.json"

        result = runner.invoke(
            app,
            [
                "install-into-claude-desktop",
                "--config-path",
                str(config_path),
            ],
        )

        assert result.exit_code == 0, result.output
        assert config_path.exists()
        # Config has our entry.
        data = json.loads(config_path.read_text())
        assert "yadirect-agent" in data["mcpServers"]
        # Output mentions what happened + the path.
        assert "added" in result.output.lower() or "installed" in result.output.lower()
        assert str(config_path) in result.output

    def test_already_installed_is_idempotent(
        self,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        config_path = tmp_path / "claude_desktop_config.json"
        runner.invoke(
            app,
            ["install-into-claude-desktop", "--config-path", str(config_path)],
        )

        result = runner.invoke(
            app,
            ["install-into-claude-desktop", "--config-path", str(config_path)],
        )

        assert result.exit_code == 0, result.output
        assert "already" in result.output.lower()

    def test_dry_run_does_not_write(
        self,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        config_path = tmp_path / "claude_desktop_config.json"

        result = runner.invoke(
            app,
            [
                "install-into-claude-desktop",
                "--config-path",
                str(config_path),
                "--dry-run",
            ],
        )

        assert result.exit_code == 0, result.output
        assert not config_path.exists()
        assert "dry" in result.output.lower() or "would" in result.output.lower()

    def test_corrupt_config_exits_nonzero(
        self,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        config_path = tmp_path / "claude_desktop_config.json"
        config_path.write_text("{ this is not json", encoding="utf-8")

        result = runner.invoke(
            app,
            ["install-into-claude-desktop", "--config-path", str(config_path)],
        )

        assert result.exit_code != 0
        assert "invalid json" in result.output.lower() or "error" in result.output.lower()

    def test_emits_restart_hint(
        self,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        # The whole point of this command is to be the LAST thing the
        # operator runs before opening Claude Desktop. The output must
        # tell them to restart Claude — without it, they install,
        # don't see the tool, and think it failed.
        config_path = tmp_path / "claude_desktop_config.json"

        result = runner.invoke(
            app,
            ["install-into-claude-desktop", "--config-path", str(config_path)],
        )

        assert result.exit_code == 0
        assert "restart" in result.output.lower()

    def test_rich_markup_in_path_does_not_inject(
        self,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        # auditor M15.2 MEDIUM-2: --config-path is operator-controlled
        # and could carry Rich markup characters. Without _rich_escape,
        # a path like ".../[red]FAKE ERROR[/red].json" renders the
        # bracketed text as styled output, misleading the operator.
        # Path must appear as literal text in the output.
        config_path = tmp_path / "[red]injected[/red].json"

        result = runner.invoke(
            app,
            ["install-into-claude-desktop", "--config-path", str(config_path)],
        )

        assert result.exit_code == 0, result.output
        # The literal "[red]" must appear in the output — if Rich
        # consumed it as markup, it would be missing.
        assert "[red]" in result.output


# --------------------------------------------------------------------------
# uninstall-from-claude-desktop
# --------------------------------------------------------------------------


class TestUninstallCmd:
    def test_removes_entry(
        self,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        config_path = tmp_path / "claude_desktop_config.json"
        runner.invoke(
            app,
            ["install-into-claude-desktop", "--config-path", str(config_path)],
        )

        result = runner.invoke(
            app,
            [
                "uninstall-from-claude-desktop",
                "--config-path",
                str(config_path),
            ],
        )

        assert result.exit_code == 0, result.output
        data = json.loads(config_path.read_text())
        assert "yadirect-agent" not in data.get("mcpServers", {})

    def test_not_installed_is_no_op(
        self,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        # Config doesn't even exist → exits clean with a clear
        # "nothing to do" message rather than crashing.
        config_path = tmp_path / "claude_desktop_config.json"

        result = runner.invoke(
            app,
            ["uninstall-from-claude-desktop", "--config-path", str(config_path)],
        )

        assert result.exit_code == 0, result.output
        assert "not installed" in result.output.lower() or "nothing" in result.output.lower()
