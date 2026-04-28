"""Rationale store — JSONL append-only sibling to plans/audit (M20.2 stub).

Implementation lands in the next commit.
"""

from __future__ import annotations

from pathlib import Path

from ..models.rationale import Rationale


class RationaleStore:
    def __init__(self, path: Path) -> None:
        self._path = path

    def append(self, rationale: Rationale) -> None:
        msg = "M20.2 — implementation in next commit"
        raise NotImplementedError(msg)

    def get(self, decision_id: str) -> Rationale | None:
        msg = "M20.2 — implementation in next commit"
        raise NotImplementedError(msg)

    def list_for_resource(self, *, campaign_id: int) -> list[Rationale]:
        msg = "M20.2 — implementation in next commit"
        raise NotImplementedError(msg)

    def list_recent(self, *, days: int) -> list[Rationale]:
        msg = "M20.2 — implementation in next commit"
        raise NotImplementedError(msg)
