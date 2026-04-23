"""Campaign management service.

All the 'smart' operations go through here — they combine multiple API
calls, validate preconditions, and emit audit events.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from ..clients.direct import DirectService
from ..config import Settings
from ..models.campaigns import Campaign, CampaignState


@dataclass(frozen=True)
class CampaignSummary:
    """Flattened view for agent consumption — no nested micro-currency fiddling."""

    id: int
    name: str
    state: str
    status: str
    type: str | None
    daily_budget_rub: float | None

    @classmethod
    def from_model(cls, c: Campaign) -> CampaignSummary:
        budget_rub: float | None = None
        if c.daily_budget is not None:
            budget_rub = c.daily_budget.amount / 1_000_000
        return cls(
            id=c.id,
            name=c.name,
            state=c.state.value if c.state else "UNKNOWN",
            status=c.status.value if c.status else "UNKNOWN",
            type=c.type,
            daily_budget_rub=budget_rub,
        )


class CampaignService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._logger = structlog.get_logger().bind(component="campaign_service")

    async def list_active(self, limit: int = 200) -> list[CampaignSummary]:
        async with DirectService(self._settings) as api:
            campaigns = await api.get_campaigns(
                states=[CampaignState.ON.value, CampaignState.SUSPENDED.value],
                limit=limit,
            )
        return [CampaignSummary.from_model(c) for c in campaigns]

    async def list_all(self, limit: int = 500) -> list[CampaignSummary]:
        async with DirectService(self._settings) as api:
            campaigns = await api.get_campaigns(limit=limit)
        return [CampaignSummary.from_model(c) for c in campaigns]

    async def pause(self, campaign_ids: list[int]) -> None:
        self._logger.info("campaigns.pause.request", ids=campaign_ids)
        async with DirectService(self._settings) as api:
            await api.suspend_campaigns(campaign_ids)
        self._logger.info("campaigns.pause.ok", ids=campaign_ids)

    async def resume(self, campaign_ids: list[int]) -> None:
        self._logger.info("campaigns.resume.request", ids=campaign_ids)
        async with DirectService(self._settings) as api:
            await api.resume_campaigns(campaign_ids)
        self._logger.info("campaigns.resume.ok", ids=campaign_ids)

    async def set_daily_budget(self, campaign_id: int, budget_rub: int) -> None:
        """Single-campaign budget update. For bulk, batch at the service level."""
        if budget_rub < 300:
            # Direct's minimum is 300 RUB. Catching early saves a round-trip.
            msg = f"Daily budget must be >= 300 RUB, got {budget_rub}"
            raise ValueError(msg)

        self._logger.info(
            "campaigns.budget.request", campaign_id=campaign_id, budget_rub=budget_rub
        )
        async with DirectService(self._settings) as api:
            await api.update_campaign_budget(campaign_id, budget_rub)
        self._logger.info("campaigns.budget.ok", campaign_id=campaign_id)
