"""Claude Desktop installer (M15.2).

The first frictionless-onboarding helper. Resolves the platform-
specific Claude Desktop config path, merges in the
``mcpServers["yadirect-agent"]`` block, and writes back atomically.
A non-developer can ``yadirect-agent install-into-claude-desktop``
once and never have to find or hand-edit JSON.

Two layers, kept separate intentionally:

1. ``resolve_config_path`` — OS-conditional. Per-platform paths:
   - macOS: ``~/Library/Application Support/Claude/...``
   - Windows: ``%APPDATA%\\Claude\\...`` (with home-dir fallback if
     ``APPDATA`` is unset, which happens in some CI containers)
   - Linux: ``$XDG_CONFIG_HOME/Claude/...`` if set, else
     ``~/.config/Claude/...``

2. ``install_into_config`` / ``uninstall_from_config`` — pure JSON
   manipulation given a path. No OS coupling, so tests can exercise
   the full merge/backup/idempotency contract under ``tmp_path``
   without monkeypatching anything heavy.

Why we refuse corrupt JSON instead of overwriting: a partially-
written config from a Claude Desktop crash, a hand-edit gone wrong,
or a third-party tool clobbering the file is a state we should NOT
silently destroy. Better to surface ``ConfigError`` and let the
operator decide whether to recover from a backup.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import structlog

_log = structlog.get_logger(component="cli.install")

# The canonical mcpServers entry we install. ``args=["mcp", "serve"]``
# matches the typer subcommand wired in ``cli/main.py:mcp_serve_cmd``.
_SERVER_NAME = "yadirect-agent"
_SERVER_ENTRY: dict[str, Any] = {
    "command": "yadirect-agent",
    "args": ["mcp", "serve"],
}

InstallAction = Literal["added", "updated", "already_installed"]
UninstallAction = Literal["removed", "not_installed"]


class ConfigError(Exception):
    """Raised when the Claude Desktop config cannot be located or parsed.

    Distinct from ``yadirect_agent.exceptions.ConfigError`` (which is
    about yadirect-agent's own settings); this one is specifically
    about the Claude Desktop config file we're modifying as a side
    effect of installation.
    """


@dataclass(frozen=True)
class InstallResult:
    config_path: Path
    action: InstallAction
    backup_path: Path | None
    dry_run: bool = False


@dataclass(frozen=True)
class UninstallResult:
    config_path: Path
    action: UninstallAction
    backup_path: Path | None
    dry_run: bool = False


def resolve_config_path() -> Path:
    """Locate the Claude Desktop config file for this OS.

    Raises:
        ConfigError: the OS is not one we support (Darwin / Windows /
            Linux). On unrecognised platforms we refuse to guess —
            the operator will need to ``--config-path`` override
            manually (a follow-up if it becomes a real need).
    """
    system = platform.system()

    if system == "Darwin":
        return (
            Path.home()
            / "Library"
            / "Application Support"
            / "Claude"
            / "claude_desktop_config.json"
        )

    if system == "Windows":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "Claude" / "claude_desktop_config.json"
        # Defensive fallback: APPDATA missing (rare on real Windows but
        # happens in CI containers). Use the standard
        # %USERPROFILE%\AppData\Roaming layout under Path.home().
        return Path.home() / "AppData" / "Roaming" / "Claude" / "claude_desktop_config.json"

    if system == "Linux":
        xdg = os.environ.get("XDG_CONFIG_HOME")
        config_root = Path(xdg) if xdg else Path.home() / ".config"
        return config_root / "Claude" / "claude_desktop_config.json"

    msg = f"unsupported platform: {system!r}"
    raise ConfigError(msg)


def _load_existing(path: Path) -> dict[str, Any] | None:
    """Read and parse the config file, or return None if absent.

    Raises ``ConfigError`` on a present-but-corrupt file. We refuse
    to silently overwrite a corrupt config — the operator may have
    a partial-write from a Claude Desktop crash that they want to
    recover, not flatten.
    """
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
        loaded = json.loads(text)
    except json.JSONDecodeError as exc:
        msg = f"existing config at {path} is invalid JSON: {exc}"
        raise ConfigError(msg) from exc
    if not isinstance(loaded, dict):
        msg = f"existing config at {path} is not a JSON object (got {type(loaded).__name__})"
        raise ConfigError(msg)
    return loaded


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """Write JSON via tempfile + os.replace for crash-safety.

    The standard ``open("w") + write + close`` pattern leaves a
    truncated config on disk if the process dies mid-write. Writing
    to a tempfile in the same dir then renaming is atomic on POSIX
    and Windows alike. The operator's Claude Desktop will keep
    reading the old config until the rename completes.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path_str = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=path.parent,
    )
    tmp_path = Path(tmp_path_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp_path, path)
    except Exception:
        # Clean up the tempfile if we never got to the replace.
        if tmp_path.exists():
            tmp_path.unlink()
        raise


def _backup_path(config_path: Path) -> Path:
    """Generate a timestamped backup path next to the config.

    Microsecond resolution avoids collisions when two installs land
    in the same second (auditor M15.2 LOW-2). Same-second collisions
    used to cause the second install to silently overwrite the
    first install's backup, losing recovery for the first.
    """
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
    return config_path.with_suffix(config_path.suffix + f".backup-{stamp}")


def _backup_existing(path: Path, dest: Path) -> None:
    """Copy ``path`` to ``dest`` preserving permission bits.

    The naive ``dest.write_bytes(path.read_bytes())`` creates the
    backup at mode 0o666 & ~umask (typically 0o644 — world-readable),
    even when the source was 0o600. On a multi-user host this leaks
    accumulated config snapshots to all local users. ``shutil.copy2``
    copies both content and permission bits in one syscall, matching
    the source mode. (auditor M15.2 MEDIUM-1.)
    """
    shutil.copy2(path, dest)


def install_into_config(path: Path, *, dry_run: bool = False) -> InstallResult:
    """Add or update the yadirect-agent entry in the Claude Desktop config.

    Returns:
        ``InstallResult`` with ``action`` indicating what (would have)
        happened: ``added`` (new entry), ``updated`` (replaced a stale
        entry), or ``already_installed`` (no-op, idempotent).

    Raises:
        ConfigError: corrupt JSON or non-object root in existing config.

    Concurrency caveat (auditor M15.2 MEDIUM-3): there is a TOCTOU
    window between the read of the existing config and the atomic
    write back. If another process (Claude Desktop itself, or a
    parallel installer for a different MCP server) writes to the
    file in that window, our write will silently overwrite their
    change. The flow is:

        1. read existing config A
        2. compute merged config A + yadirect-agent
        3. backup, atomic-write (A + yadirect-agent)

    If between steps 1 and 3 another process writes B (where B
    contains entries A doesn't), step 3 silently drops those
    entries. For human-paced operator action this race window is
    sub-millisecond and effectively unreachable, but a provisioning
    script that runs ``install-into-claude-desktop`` in parallel
    with another MCP installer is at risk. Same operational model
    as ``apply-plan`` (single-operator local trust, no fcntl.flock);
    fix when multi-process concurrency becomes a real requirement.
    """
    existing = _load_existing(path)
    is_new_file = existing is None
    config: dict[str, Any] = existing.copy() if existing else {}

    servers = config.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
    servers = dict(servers)  # shallow copy so we don't mutate the original

    current_entry = servers.get(_SERVER_NAME)
    if current_entry == _SERVER_ENTRY:
        action: InstallAction = "already_installed"
    elif current_entry is None:
        action = "added"
    else:
        action = "updated"

    # Compose the new config.
    servers[_SERVER_NAME] = _SERVER_ENTRY
    config["mcpServers"] = servers

    backup_path: Path | None = None
    if action == "already_installed":
        # Nothing changed; no write, no backup.
        return InstallResult(
            config_path=path,
            action=action,
            backup_path=None,
            dry_run=dry_run,
        )

    if dry_run:
        return InstallResult(
            config_path=path,
            action=action,
            backup_path=None,
            dry_run=True,
        )

    # Real write. Back up the existing file first if it had any prior
    # content (action != "added" implies we either updated a stale
    # entry or merged into an existing config — both cases preserve
    # data we want recoverable). For "added" with a brand-new file
    # there is nothing to back up.
    if not is_new_file:
        backup_path = _backup_path(path)
        _backup_existing(path, backup_path)
        _log.info(
            "claude_desktop.config.backed_up",
            original=str(path),
            backup=str(backup_path),
        )

    _atomic_write_json(path, config)
    _log.info(
        "claude_desktop.config.installed",
        path=str(path),
        action=action,
    )

    return InstallResult(
        config_path=path,
        action=action,
        backup_path=backup_path,
        dry_run=False,
    )


def uninstall_from_config(path: Path, *, dry_run: bool = False) -> UninstallResult:
    """Remove the yadirect-agent entry from the Claude Desktop config.

    No-op (``action="not_installed"``) when the config file is missing
    or the entry is absent. Other MCP servers and unrelated top-level
    fields are preserved verbatim.
    """
    existing = _load_existing(path)
    if existing is None:
        return UninstallResult(
            config_path=path,
            action="not_installed",
            backup_path=None,
            dry_run=dry_run,
        )

    servers = existing.get("mcpServers")
    if not isinstance(servers, dict) or _SERVER_NAME not in servers:
        return UninstallResult(
            config_path=path,
            action="not_installed",
            backup_path=None,
            dry_run=dry_run,
        )

    if dry_run:
        return UninstallResult(
            config_path=path,
            action="removed",
            backup_path=None,
            dry_run=True,
        )

    # Compose the new config.
    new_servers = {k: v for k, v in servers.items() if k != _SERVER_NAME}
    new_config = existing.copy()
    new_config["mcpServers"] = new_servers

    backup_path = _backup_path(path)
    _backup_existing(path, backup_path)
    _log.info(
        "claude_desktop.config.backed_up",
        original=str(path),
        backup=str(backup_path),
    )
    _atomic_write_json(path, new_config)
    _log.info("claude_desktop.config.uninstalled", path=str(path))

    return UninstallResult(
        config_path=path,
        action="removed",
        backup_path=backup_path,
        dry_run=False,
    )
