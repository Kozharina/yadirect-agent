"""Tests for CampaignService.

These tests exercise the service's **decisions**: which states it queries,
how it turns wire-shaped Campaign objects into flattened CampaignSummary,
and what it refuses to do. HTTP is stubbed at the DirectService boundary
via monkeypatch — see docs/TESTING.md for rationale.
"""

from __future__ import annotations

from typing import Any

import pytest

from yadirect_agent.clients import direct as direct_module
from yadirect_agent.config import Settings
from yadirect_agent.models.campaigns import (
    Campaign,
    CampaignState,
    CampaignStatus,
    DailyBudget,
)
from yadirect_agent.services.campaigns import CampaignService, CampaignSummary

# --------------------------------------------------------------------------
# In-memory stub that replaces DirectService.
# --------------------------------------------------------------------------


class _FakeDirectService:
    """Captures calls and replays scripted results.

    Behaves as an async context manager so `async with DirectService(...) as api`
    works unchanged in production code.
    """

    def __init__(
        self,
        *,
        campaigns: list[Campaign] | None = None,
    ) -> None:
        self._campaigns = campaigns or []
        self.suspend_calls: list[list[int]] = []
        self.resume_calls: list[list[int]] = []
        self.budget_calls: list[tuple[int, int, str]] = []
        self.get_campaigns_kwargs: dict[str, Any] | None = None

    async def __aenter__(self) -> _FakeDirectService:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    async def get_campaigns(
        self,
        ids: list[int] | None = None,
        states: list[str] | None = None,
        types: list[str] | None = None,
        limit: int = 500,
    ) -> list[Campaign]:
        self.get_campaigns_kwargs = {
            "ids": ids,
            "states": states,
            "types": types,
            "limit": limit,
        }
        return list(self._campaigns)

    async def suspend_campaigns(self, ids: list[int]) -> dict[str, Any]:
        self.suspend_calls.append(list(ids))
        return {}

    async def resume_campaigns(self, ids: list[int]) -> dict[str, Any]:
        self.resume_calls.append(list(ids))
        return {}

    async def update_campaign_budget(
        self, campaign_id: int, daily_budget_rub: int, mode: str = "STANDARD"
    ) -> dict[str, Any]:
        self.budget_calls.append((campaign_id, daily_budget_rub, mode))
        return {}


@pytest.fixture
def fake_direct(monkeypatch: pytest.MonkeyPatch) -> _FakeDirectService:
    """Patches DirectService in clients.direct and services.campaigns lookups."""
    fake = _FakeDirectService()

    def _factory(_settings: Settings) -> _FakeDirectService:
        return fake

    # CampaignService does `from ..clients.direct import DirectService`, so we
    # patch the symbol where it is *used* (services.campaigns) in addition to
    # the source module — standard monkeypatch gotcha. Use a dotted-path string
    # so we avoid mixing `import X` and `from X import Y` forms for the same
    # module (CodeQL py/unnecessary-import-alias).
    monkeypatch.setattr("yadirect_agent.services.campaigns.DirectService", _factory)
    monkeypatch.setattr(direct_module, "DirectService", _factory)
    return fake


# --------------------------------------------------------------------------
# CampaignSummary: pure mapping logic.
# --------------------------------------------------------------------------


class TestCampaignSummary:
    def test_converts_micro_currency_to_rubles(self) -> None:
        c = Campaign(
            Id=1,
            Name="c1",
            State=CampaignState.ON,
            Status=CampaignStatus.ACCEPTED,
            Type="TEXT_CAMPAIGN",
            DailyBudget=DailyBudget(amount=500_000_000, mode="STANDARD"),
        )

        summary = CampaignSummary.from_model(c)

        assert summary.id == 1
        assert summary.name == "c1"
        assert summary.state == "ON"
        assert summary.status == "ACCEPTED"
        assert summary.daily_budget_rub == 500.0

    def test_missing_budget_yields_none(self) -> None:
        c = Campaign(Id=2, Name="c2", State=CampaignState.OFF)

        summary = CampaignSummary.from_model(c)

        assert summary.daily_budget_rub is None

    def test_missing_state_and_status_become_unknown(self) -> None:
        c = Campaign(Id=3, Name="c3")

        summary = CampaignSummary.from_model(c)

        assert summary.state == "UNKNOWN"
        assert summary.status == "UNKNOWN"


# --------------------------------------------------------------------------
# list_active / list_all: filter semantics.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_active_queries_on_and_suspended_states(
    settings: Settings, fake_direct: _FakeDirectService
) -> None:
    await CampaignService(settings).list_active()

    assert fake_direct.get_campaigns_kwargs is not None
    assert set(fake_direct.get_campaigns_kwargs["states"]) == {"ON", "SUSPENDED"}


@pytest.mark.asyncio
async def test_list_active_flattens_to_summaries(
    settings: Settings, fake_direct: _FakeDirectService
) -> None:
    fake_direct._campaigns = [
        Campaign(
            Id=10,
            Name="alpha",
            State=CampaignState.ON,
            Status=CampaignStatus.ACCEPTED,
            DailyBudget=DailyBudget(amount=1_000_000_000, mode="STANDARD"),
        ),
        Campaign(Id=11, Name="beta", State=CampaignState.SUSPENDED),
    ]

    summaries = await CampaignService(settings).list_active()

    assert [s.id for s in summaries] == [10, 11]
    assert summaries[0].daily_budget_rub == 1000.0
    assert summaries[1].daily_budget_rub is None


@pytest.mark.asyncio
async def test_list_all_does_not_filter_states(
    settings: Settings, fake_direct: _FakeDirectService
) -> None:
    await CampaignService(settings).list_all()

    assert fake_direct.get_campaigns_kwargs is not None
    assert fake_direct.get_campaigns_kwargs["states"] is None


# --------------------------------------------------------------------------
# pause / resume: pass-through of IDs.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pause_delegates_to_client_suspend(
    settings: Settings, fake_direct: _FakeDirectService
) -> None:
    await CampaignService(settings).pause([1, 2, 3])

    assert fake_direct.suspend_calls == [[1, 2, 3]]
    assert fake_direct.resume_calls == []


@pytest.mark.asyncio
async def test_resume_delegates_to_client_resume(
    settings: Settings, fake_direct: _FakeDirectService
) -> None:
    await CampaignService(settings).resume([7])

    assert fake_direct.resume_calls == [[7]]
    assert fake_direct.suspend_calls == []


# --------------------------------------------------------------------------
# set_daily_budget: enforces Direct's minimum.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_daily_budget_rejects_below_minimum(
    settings: Settings, fake_direct: _FakeDirectService
) -> None:
    with pytest.raises(ValueError, match=">= 300 RUB"):
        await CampaignService(settings).set_daily_budget(campaign_id=1, budget_rub=299)

    # We rejected early — the client must not have been called.
    assert fake_direct.budget_calls == []


@pytest.mark.asyncio
async def test_set_daily_budget_accepts_minimum(
    settings: Settings, fake_direct: _FakeDirectService
) -> None:
    await CampaignService(settings).set_daily_budget(campaign_id=42, budget_rub=300)

    assert fake_direct.budget_calls == [(42, 300, "STANDARD")]


@pytest.mark.asyncio
async def test_set_daily_budget_passes_through_amount_in_rubles(
    settings: Settings, fake_direct: _FakeDirectService
) -> None:
    # The service speaks rubles; the client is responsible for converting to
    # micro-currency. We verify that contract here.
    await CampaignService(settings).set_daily_budget(campaign_id=42, budget_rub=1500)

    assert fake_direct.budget_calls == [(42, 1500, "STANDARD")]
