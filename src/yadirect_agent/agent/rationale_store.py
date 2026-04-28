"""Rationale store — JSONL append-only sibling to plans/audit (M20.2).

Stores ``Rationale`` records, one per JSON line. Same operational
contract as ``PendingPlansStore``:

- Append-only: callers never mutate existing rows. ``append`` always
  writes a new line. Two writes with the same ``decision_id`` keep
  both on disk; reader semantics are last-write-wins.
- Tamper-evident: the on-disk log is a complete audit trail of
  every rationale ever recorded, even superseded ones. A future
  M20 slice will bridge this into the central audit pipeline.
- Defensive parsing: a corrupt line does not invalidate the rest
  of the file. Skipped silently, structlog warning emitted (logged
  once per scan rather than once per skip — keeps logs readable).
- Missing file is normal: a fresh deployment has no history,
  reads return empty / None.

The store lives at ``rationale.jsonl`` next to ``audit.jsonl`` and
``pending_plans.jsonl``. Path is operator-configurable via the
constructor — every M-feature passes its own ``Settings`` and picks
the location.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import structlog

from ..models.rationale import Rationale

_log = structlog.get_logger(component="agent.rationale_store")


class RationaleStore:
    """JSONL append-only store of ``Rationale`` entries."""

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    # -- Writes ----------------------------------------------------------

    def append(self, rationale: Rationale) -> None:
        """Append ``rationale`` as a JSON line. Creates parent dirs on demand."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as f:
            f.write(rationale.model_dump_json() + "\n")

    # -- Reads -----------------------------------------------------------

    def get(self, decision_id: str) -> Rationale | None:
        """Latest rationale for ``decision_id``, or None if unknown.

        On a missing file or unknown id, returns None — fresh
        deployments and stale links have a clean failure mode.
        """
        return self._collapse_by_id().get(decision_id)

    def list_for_resource(self, *, campaign_id: int) -> list[Rationale]:
        """All rationales whose ``resource_ids`` include ``campaign_id``.

        Sorted newest-first so an operator asking "what touched
        campaign 42 lately?" gets recent decisions at the top.
        """
        results = [r for r in self._collapse_by_id().values() if campaign_id in r.resource_ids]
        results.sort(key=lambda r: r.timestamp, reverse=True)
        return results

    def list_recent(self, *, days: int) -> list[Rationale]:
        """Rationales recorded within the last ``days`` days, newest first.

        Window is anchored to ``datetime.now(UTC)`` minus ``days`` days.
        ``days <= 0`` raises ValueError — empty-window queries are
        almost always a CLI typo, not a real intent ("show me the
        last zero days of decisions" makes no sense). Callers wanting
        unbounded history use ``_collapse_by_id`` directly through a
        future API, not this helper.
        """
        if days <= 0:
            msg = f"days must be positive, got {days}"
            raise ValueError(msg)
        cutoff = datetime.now(UTC) - timedelta(days=days)
        results = [r for r in self._collapse_by_id().values() if r.timestamp >= cutoff]
        results.sort(key=lambda r: r.timestamp, reverse=True)
        return results

    # -- Internals -------------------------------------------------------

    def _collapse_by_id(self) -> dict[str, Rationale]:
        """Scan the file, keep the latest entry per decision_id."""
        if not self._path.exists():
            return {}
        out: dict[str, Rationale] = {}
        skipped = 0
        with self._path.open(encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    rationale = Rationale.model_validate_json(line)
                except (json.JSONDecodeError, ValueError):
                    # Corrupt line — could be a partial write from a
                    # crashed process, a hand-edit, or a future format
                    # change we don't yet understand. Skip and keep
                    # parsing; the rest of the file is still useful.
                    skipped += 1
                    continue
                out[rationale.decision_id] = rationale
        if skipped > 0:
            _log.warning(
                "rationale.store.corrupt_lines_skipped",
                path=str(self._path),
                skipped=skipped,
            )
        return out
