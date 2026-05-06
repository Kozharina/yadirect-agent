"""Append-only JSONL store for ``HealthSnapshot`` rows (M15.5.5).

Sibling to ``RationaleStore`` (``rationale.jsonl``), ``CostStore``
(``cost.jsonl``), and ``PendingPlansStore`` (``pending_plans.jsonl``)
— same operational contract every persistent store in this codebase
has settled on:

- Append-only: callers never mutate existing rows. Each
  ``run_account_check`` invocation appends one line per active
  campaign. Latest-wins semantics on read collapse repeats.
- Defensive parsing: a corrupt line does not invalidate the rest
  of the file. Skipped silently with a structlog warning.
- Missing file is normal: a fresh deployment has no history;
  ``load_latest_per_campaign`` returns ``{}`` and historical
  rules silently skip on the first run.

Why append + collapse, not whole-file rewrite-the-latest:

- Append is atomic on POSIX for writes < PIPE_BUF (~4KB on Linux,
  512 bytes on macOS). One snapshot line is well under that.
- Whole-file rewrite would need a tempfile + ``os.replace`` dance
  to stay atomic. More moving parts for no real benefit at this
  scale (10s of campaigns x 1 snapshot per check = trivially small).
- The full append-only log is its own audit trail: an operator
  asking "what was the CTR three weeks ago?" has the answer in
  the file even though the rule only reads the latest. Promote
  to a query helper if anyone actually asks.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from ..models.health_history import HealthSnapshot

if TYPE_CHECKING:
    from ..config import Settings

_log = structlog.get_logger(component="services.health_history_store")


class HealthHistoryStore:
    """JSONL append-only store of ``HealthSnapshot`` entries."""

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    @classmethod
    def from_settings(cls, settings: Settings) -> HealthHistoryStore:
        """Conventional path: sibling of ``audit.jsonl``.

        One source of truth for the path so a future move of the
        log directory only touches one place. Mirrors
        ``RationaleStore.from_settings`` (when extracted; for now
        the convention is duplicated at call sites — see
        BACKLOG tech-debt note).
        """
        return cls(settings.audit_log_path.parent / "health_history.jsonl")

    # -- Writes ----------------------------------------------------------

    def append(self, snapshots: list[HealthSnapshot]) -> None:
        """Append a batch of snapshots, one JSON line each.

        Empty list is a no-op (no parent-dir creation, no file
        touch) — keeps a fresh-account ``run_account_check`` with
        zero campaigns truly silent on disk.
        """
        if not snapshots:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as f:
            for snap in snapshots:
                f.write(json.dumps(snap.to_jsonable()) + "\n")

    # -- Reads -----------------------------------------------------------

    def load_latest_per_campaign(self) -> dict[int, HealthSnapshot]:
        """Return the newest-by-``snapshot_at`` snapshot per campaign.

        On missing file, returns an empty dict. On corrupt lines,
        skips them and emits one aggregated warning per scan
        (mirroring ``RationaleStore._collapse_by_id``).
        """
        if not self._path.exists():
            return {}

        per_campaign: dict[int, list[HealthSnapshot]] = defaultdict(list)
        skipped = 0
        with self._path.open(encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    snap = HealthSnapshot.from_jsonable(data)
                except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                    skipped += 1
                    continue
                per_campaign[snap.campaign_id].append(snap)

        if skipped > 0:
            _log.warning(
                "health_history.load.skipped_corrupt_lines",
                count=skipped,
                path=str(self._path),
            )

        return {
            campaign_id: max(snaps, key=lambda s: s.snapshot_at)
            for campaign_id, snaps in per_campaign.items()
        }


__all__ = ["HealthHistoryStore"]
