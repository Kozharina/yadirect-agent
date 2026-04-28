"""Tests for the Claude Desktop installer (M15.2).

Two layers, kept in this one file because they're tightly coupled:

1. ``resolve_config_path``: cross-platform path resolver. Tested by
   monkeypatching ``platform.system()`` and the home dir so we never
   touch a real config.

2. ``install_into_config`` / ``uninstall_from_config``: pure JSON
   manipulation given a path. Tested with ``tmp_path`` fixtures so
   the merge / backup / idempotency contracts can be pinned without
   any OS-level coupling.

Why this split: the path resolver is OS-conditional but the JSON
manipulation isn't. Mixing them into one test surface would tangle
two concerns; keeping them separate means a Windows test can
exercise the Linux path under monkeypatch with confidence.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from yadirect_agent.cli.install import (
    ConfigError,
    install_into_config,
    resolve_config_path,
    uninstall_from_config,
)

# --------------------------------------------------------------------------
# resolve_config_path — cross-platform.
# --------------------------------------------------------------------------


class TestResolveConfigPath:
    def test_macos(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr("platform.system", lambda: "Darwin")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        path = resolve_config_path()

        assert path == (
            tmp_path / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
        )

    def test_windows_with_appdata(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setattr("platform.system", lambda: "Windows")
        appdata = tmp_path / "AppData" / "Roaming"
        appdata.mkdir(parents=True)
        monkeypatch.setenv("APPDATA", str(appdata))

        path = resolve_config_path()

        assert path == appdata / "Claude" / "claude_desktop_config.json"

    def test_windows_without_appdata_falls_back_to_home(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # Defensive: APPDATA is normally set on Windows but we should
        # not crash if it isn't (rare, but possible in CI containers).
        monkeypatch.setattr("platform.system", lambda: "Windows")
        monkeypatch.delenv("APPDATA", raising=False)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        path = resolve_config_path()

        # Some sensible fallback under the home dir, not a crash.
        assert tmp_path in path.parents
        assert path.name == "claude_desktop_config.json"

    def test_linux(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr("platform.system", lambda: "Linux")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

        path = resolve_config_path()

        assert path == tmp_path / ".config" / "Claude" / "claude_desktop_config.json"

    def test_linux_respects_xdg_config_home(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setattr("platform.system", lambda: "Linux")
        custom = tmp_path / "my-config"
        custom.mkdir()
        monkeypatch.setenv("XDG_CONFIG_HOME", str(custom))

        path = resolve_config_path()

        assert path == custom / "Claude" / "claude_desktop_config.json"

    def test_unknown_os_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("platform.system", lambda: "BeOS")

        with pytest.raises(ConfigError, match="unsupported"):
            resolve_config_path()


# --------------------------------------------------------------------------
# install_into_config — pure JSON manipulation.
# --------------------------------------------------------------------------


class TestInstallIntoConfig:
    def _read_config(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    def test_writes_new_config_when_missing(self, tmp_path: Path) -> None:
        path = tmp_path / "claude_desktop_config.json"
        # File does not exist — Claude Desktop is fresh-installed but
        # the user has not opened it yet.

        result = install_into_config(path)

        assert path.exists()
        config = self._read_config(path)
        assert "mcpServers" in config
        assert "yadirect-agent" in config["mcpServers"]
        # Server entry has the right shape.
        entry = config["mcpServers"]["yadirect-agent"]
        assert entry["command"] == "yadirect-agent"
        assert entry["args"] == ["mcp", "serve"]
        # Result reports what happened.
        assert result.action == "added"
        assert result.config_path == path

    def test_merges_into_existing_config(self, tmp_path: Path) -> None:
        # User already has another MCP server configured (e.g. they
        # use Claude Desktop with multiple integrations). Our install
        # MUST NOT clobber it.
        path = tmp_path / "claude_desktop_config.json"
        existing = {
            "mcpServers": {
                "filesystem": {
                    "command": "mcp-server-filesystem",
                    "args": ["--root", "/Users/anna"],
                },
            },
            "globalShortcut": "Cmd+Shift+Y",  # unrelated top-level field
        }
        path.write_text(json.dumps(existing), encoding="utf-8")

        install_into_config(path)

        config = self._read_config(path)
        # Existing server preserved.
        assert "filesystem" in config["mcpServers"]
        assert config["mcpServers"]["filesystem"]["command"] == "mcp-server-filesystem"
        # Our server added alongside.
        assert "yadirect-agent" in config["mcpServers"]
        # Unrelated top-level field preserved.
        assert config["globalShortcut"] == "Cmd+Shift+Y"

    def test_creates_timestamped_backup(self, tmp_path: Path) -> None:
        path = tmp_path / "claude_desktop_config.json"
        original = {"mcpServers": {"other": {"command": "x"}}}
        path.write_text(json.dumps(original), encoding="utf-8")

        result = install_into_config(path)

        # Backup file exists with timestamp suffix.
        assert result.backup_path is not None
        assert result.backup_path.exists()
        assert "backup-" in result.backup_path.name
        # Backup contains the original config verbatim.
        assert json.loads(result.backup_path.read_text(encoding="utf-8")) == original

    def test_no_backup_when_config_did_not_exist(self, tmp_path: Path) -> None:
        # Fresh install with no prior config — nothing to back up.
        path = tmp_path / "claude_desktop_config.json"

        result = install_into_config(path)

        assert result.backup_path is None

    def test_idempotent_when_already_installed(self, tmp_path: Path) -> None:
        path = tmp_path / "claude_desktop_config.json"
        install_into_config(path)
        first = self._read_config(path)

        # Second run on already-installed config.
        result2 = install_into_config(path)
        second = self._read_config(path)

        # Config unchanged — same single yadirect-agent entry.
        assert first == second
        assert result2.action == "already_installed"
        # No backup needed when nothing changed.
        assert result2.backup_path is None

    def test_overwrites_existing_yadirect_entry(self, tmp_path: Path) -> None:
        # Edge case: a user has manually added a yadirect-agent entry
        # with stale args. Our install replaces it with the canonical
        # shape (and reports action="updated").
        path = tmp_path / "claude_desktop_config.json"
        stale = {
            "mcpServers": {
                "yadirect-agent": {
                    "command": "yadirect-agent",
                    "args": ["--old-flag"],
                },
            },
        }
        path.write_text(json.dumps(stale), encoding="utf-8")

        result = install_into_config(path)

        config = self._read_config(path)
        assert config["mcpServers"]["yadirect-agent"]["args"] == ["mcp", "serve"]
        assert result.action == "updated"
        # Backup of the stale shape kept on disk.
        assert result.backup_path is not None

    def test_dry_run_does_not_write(self, tmp_path: Path) -> None:
        path = tmp_path / "claude_desktop_config.json"

        result = install_into_config(path, dry_run=True)

        # File still doesn't exist (dry run = pure preview).
        assert not path.exists()
        # Result still describes what WOULD happen.
        assert result.action == "added"
        assert result.dry_run is True

    def test_corrupt_json_aborts_with_clear_error(self, tmp_path: Path) -> None:
        path = tmp_path / "claude_desktop_config.json"
        path.write_text("{ this is not valid json", encoding="utf-8")

        # Refuse to overwrite a corrupt config silently — better to
        # surface the error and let the operator fix it manually.
        with pytest.raises(ConfigError, match="invalid JSON"):
            install_into_config(path)


# --------------------------------------------------------------------------
# uninstall_from_config — reverse direction.
# --------------------------------------------------------------------------


class TestUninstallFromConfig:
    def _read_config(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    def test_removes_yadirect_entry(self, tmp_path: Path) -> None:
        path = tmp_path / "claude_desktop_config.json"
        install_into_config(path)

        result = uninstall_from_config(path)

        config = self._read_config(path)
        assert "yadirect-agent" not in config.get("mcpServers", {})
        assert result.action == "removed"

    def test_preserves_other_servers(self, tmp_path: Path) -> None:
        path = tmp_path / "claude_desktop_config.json"
        existing = {
            "mcpServers": {
                "filesystem": {"command": "x"},
                "yadirect-agent": {"command": "yadirect-agent", "args": ["mcp", "serve"]},
                "other": {"command": "y"},
            },
        }
        path.write_text(json.dumps(existing), encoding="utf-8")

        uninstall_from_config(path)

        config = self._read_config(path)
        assert set(config["mcpServers"].keys()) == {"filesystem", "other"}

    def test_no_op_when_not_installed(self, tmp_path: Path) -> None:
        path = tmp_path / "claude_desktop_config.json"
        existing = {"mcpServers": {"filesystem": {"command": "x"}}}
        path.write_text(json.dumps(existing), encoding="utf-8")

        result = uninstall_from_config(path)

        assert result.action == "not_installed"
        # Config untouched.
        config = self._read_config(path)
        assert config == existing

    def test_no_op_when_config_missing(self, tmp_path: Path) -> None:
        path = tmp_path / "claude_desktop_config.json"

        result = uninstall_from_config(path)

        assert result.action == "not_installed"
        assert not path.exists()  # Did not create an empty config.

    def test_dry_run_does_not_write(self, tmp_path: Path) -> None:
        path = tmp_path / "claude_desktop_config.json"
        install_into_config(path)
        before = self._read_config(path)

        result = uninstall_from_config(path, dry_run=True)

        after = self._read_config(path)
        assert before == after  # Unchanged.
        assert result.action == "removed"
        assert result.dry_run is True
