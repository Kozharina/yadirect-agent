"""Per-campaign CTR snapshot model for week-over-week comparison (M15.5.5).

The CTR-drift rule needs last-week's per-campaign CTR to compute
a drop. We persist these snapshots in an append-only JSONL store
(``HealthHistoryStore``) and read them back at the start of every
``HealthCheckService.run_account_check`` invocation.

Design choices, mirroring ``models/health.py``:

- Frozen dataclass, not pydantic. Snapshots are produced internally
  by ``HealthCheckService`` from already-validated ``CampaignPerformance``
  rows; never deserialised from an untrusted wire. Frozen catches
  the "let me bump ctr_pct for the demo" anti-pattern at the type
  level.
- ``ctr_pct`` is explicitly Optional. When ``impressions == 0`` the
  CTR is undefined (not zero, not infinity). Consumers MUST treat
  None as "unknown / not applicable", never default it to 0 — that
  would silently turn a no-traffic campaign into "CTR dropped 100%".
- ``snapshot_at`` is the wall-clock moment the snapshot was taken
  (not the end of the date range). Two checks of the same week
  produce two snapshots with different ``snapshot_at`` and the
  store collapses to the newest one.
- ``date_range`` records what window the snapshot summarises so a
  reader can tell "last week" from "this week" without trusting
  ``snapshot_at`` order alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from .metrika import DateRange


@dataclass(frozen=True)
class HealthSnapshot:
    """One per-campaign CTR snapshot persisted across checks."""

    snapshot_at: datetime
    date_range: DateRange
    campaign_id: int
    clicks: int
    impressions: int
    ctr_pct: float | None

    def __post_init__(self) -> None:
        # Same defensive shape as ``CampaignPerformance.__post_init__``:
        # frozen dataclasses don't enforce field types, so a future
        # deserialization path (``from_jsonable``) could sneak in
        # negative values that invert downstream rule semantics.
        # Pin at the boundary.
        if self.clicks < 0:
            msg = f"clicks must be non-negative, got {self.clicks}"
            raise ValueError(msg)
        if self.impressions < 0:
            msg = f"impressions must be non-negative, got {self.impressions}"
            raise ValueError(msg)

    def to_jsonable(self) -> dict[str, Any]:
        """Render as a JSON-serialisable dict for ``HealthHistoryStore``.

        Date components are ISO strings — human-greppable inside
        the JSONL file. Round-trips losslessly through
        ``from_jsonable``.
        """
        return {
            "snapshot_at": self.snapshot_at.isoformat(),
            "date_range": {
                "start": self.date_range.start.isoformat(),
                "end": self.date_range.end.isoformat(),
            },
            "campaign_id": self.campaign_id,
            "clicks": self.clicks,
            "impressions": self.impressions,
            "ctr_pct": self.ctr_pct,
        }

    @classmethod
    def from_jsonable(cls, data: dict[str, Any]) -> HealthSnapshot:
        """Inverse of ``to_jsonable``. Raises if the dict is malformed.

        Used by ``HealthHistoryStore`` per-line; a parse error is
        caught up there and the line is skipped (corrupt-line
        tolerance), rather than propagating from here.
        """
        return cls(
            snapshot_at=datetime.fromisoformat(data["snapshot_at"]),
            date_range=DateRange(
                start=date.fromisoformat(data["date_range"]["start"]),
                end=date.fromisoformat(data["date_range"]["end"]),
            ),
            campaign_id=int(data["campaign_id"]),
            clicks=int(data["clicks"]),
            impressions=int(data["impressions"]),
            ctr_pct=None if data["ctr_pct"] is None else float(data["ctr_pct"]),
        )


__all__ = ["HealthSnapshot"]
