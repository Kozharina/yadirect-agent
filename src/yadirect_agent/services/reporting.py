"""Reporting service: joins Direct cost/clicks with Metrika conversions.

Stub for M6 basic — implementation lands in the next commit. The class
exists here so the test module can import it and the in-test monkeypatch
on ``MetrikaService`` has a name to bind against.

Design:

- One source of truth for the join — the Metrika ``/stat/v1/data``
  endpoint. Metrika ingests Direct cost/click data via the Direct↔
  Metrika integration (``ym:ad:directCost``, ``ym:s:visits``), so
  fetching everything from Metrika gives a consistent snapshot
  without parsing Direct's TSV reports.
- A few-hour data lag is acceptable for first-look / health-check
  use cases (M15.5). Sub-hour freshness will need Direct's
  ``fetch_report`` (TSV, async polling) — out of scope for M6 basic.
- ``cpa_rub`` and ``cr_pct`` are computed here, not by callers, so
  the divide-by-zero contract is enforced in one place.
"""

from __future__ import annotations

from typing import Self

from ..clients.metrika import MetrikaService
from ..config import Settings
from ..models.metrika import CampaignPerformance, DateRange


class ReportingService:
    """Thin facade over MetrikaService that produces joined DTOs."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    async def campaign_performance(
        self,
        *,
        campaign_id: int,
        campaign_name: str,
        date_range: DateRange,
        goal_id: int | None = None,
    ) -> CampaignPerformance:
        """One campaign's clicks / cost / conversions over the window.

        Returns zero-filled ``CampaignPerformance`` when Metrika has
        no data for the campaign in the window (new, paused, or no
        traffic). ``cpa_rub`` and ``cr_pct`` are None whenever they
        would be undefined (zero conversions, zero clicks, zero cost).
        """
        msg = "M6 basic — implementation in next commit"
        raise NotImplementedError(msg)


# Re-export so monkeypatch in tests can target a stable name.
__all__ = ["MetrikaService", "ReportingService"]
