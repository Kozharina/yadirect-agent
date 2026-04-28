"""Claude Desktop installer (M15.2) — stub.

Implementation lands in the next commit.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal


class ConfigError(Exception):
    """Raised when the Claude Desktop config cannot be located or parsed.

    Distinct from yadirect_agent.exceptions.ConfigError (which is about
    yadirect-agent's own settings); this one is specifically about the
    Claude Desktop config file we're modifying as a side effect.
    """


InstallAction = Literal["added", "updated", "already_installed"]
UninstallAction = Literal["removed", "not_installed"]


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
    msg = "M15.2 — implementation in next commit"
    raise NotImplementedError(msg)


def install_into_config(path: Path, *, dry_run: bool = False) -> InstallResult:
    msg = "M15.2 — implementation in next commit"
    raise NotImplementedError(msg)


def uninstall_from_config(path: Path, *, dry_run: bool = False) -> UninstallResult:
    msg = "M15.2 — implementation in next commit"
    raise NotImplementedError(msg)
