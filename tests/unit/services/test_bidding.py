"""Tests for BiddingService.

Backfill for an existing contract — the code already does rubles→micro
conversion and empty-list no-op; these tests pin that contract so a
regression shows up as red, not as a silent misbehaviour on a live
account. No behaviour change here, exempt per
docs/TESTING.md #what_counts_as_behaviour_change.

Strategy: monkeypatch DirectService (same pattern as test_campaigns.py)
so no HTTP is touched. We observe what arrives at `set_keyword_bids`
and assert on it.
"""

from __future__ import annotations

from typing import Any

import pytest

from yadirect_agent.clients import direct as direct_module
from yadirect_agent.config import Settings
from yadirect_agent.models.keywords import KeywordBid
from yadirect_agent.services.bidding import BiddingService, BidUpdate

# --------------------------------------------------------------------------
# In-memory DirectService stub.
# --------------------------------------------------------------------------


class _FakeDirectService:
    """Captures the payload that reaches set_keyword_bids."""

    def __init__(self) -> None:
        self.set_keyword_bids_calls: list[list[KeywordBid]] = []

    async def __aenter__(self) -> _FakeDirectService:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    async def set_keyword_bids(self, bids: list[KeywordBid]) -> dict[str, Any]:
        # Copy so later mutations in the service don't rewrite captures.
        self.set_keyword_bids_calls.append(list(bids))
        return {}


@pytest.fixture
def fake_direct(monkeypatch: pytest.MonkeyPatch) -> _FakeDirectService:
    fake = _FakeDirectService()

    def _factory(_settings: Settings) -> _FakeDirectService:
        return fake

    # Patch both the source module and the site where BiddingService imports
    # DirectService — same gotcha as in tests/unit/services/test_campaigns.py.
    monkeypatch.setattr("yadirect_agent.services.bidding.DirectService", _factory)
    monkeypatch.setattr(direct_module, "DirectService", _factory)
    return fake


# --------------------------------------------------------------------------
# apply(): empty list short-circuit.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_empty_list_is_a_noop(
    settings: Settings, fake_direct: _FakeDirectService
) -> None:
    # Empty list must not reach the API at all — we don't want to pay the
    # network round-trip or a units charge for nothing.
    await BiddingService(settings).apply([], _applying_plan_id="test-bypass")

    assert fake_direct.set_keyword_bids_calls == []


# --------------------------------------------------------------------------
# apply(): rubles → micro-currency conversion.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_single_search_bid_converts_rubles_to_micro(
    settings: Settings, fake_direct: _FakeDirectService
) -> None:
    # 10.50 RUB → 10_500_000 micro.
    await BiddingService(settings).apply(
        [BidUpdate(keyword_id=42, new_search_bid_rub=10.50)], _applying_plan_id="test-bypass"
    )

    [[bid]] = fake_direct.set_keyword_bids_calls
    assert bid.keyword_id == 42
    assert bid.search_bid == 10_500_000
    assert bid.network_bid is None


@pytest.mark.asyncio
async def test_apply_single_network_bid_converts_rubles_to_micro(
    settings: Settings, fake_direct: _FakeDirectService
) -> None:
    await BiddingService(settings).apply(
        [BidUpdate(keyword_id=7, new_network_bid_rub=3.0)], _applying_plan_id="test-bypass"
    )

    [[bid]] = fake_direct.set_keyword_bids_calls
    assert bid.keyword_id == 7
    assert bid.search_bid is None
    assert bid.network_bid == 3_000_000


@pytest.mark.asyncio
async def test_apply_both_search_and_network_bids(
    settings: Settings, fake_direct: _FakeDirectService
) -> None:
    await BiddingService(settings).apply(
        [BidUpdate(keyword_id=1, new_search_bid_rub=5.0, new_network_bid_rub=2.0)],
        _applying_plan_id="test-bypass",
    )

    [[bid]] = fake_direct.set_keyword_bids_calls
    assert bid.search_bid == 5_000_000
    assert bid.network_bid == 2_000_000


@pytest.mark.asyncio
async def test_apply_with_both_bids_none_passes_nones_through(
    settings: Settings, fake_direct: _FakeDirectService
) -> None:
    # Degenerate input: a keyword update with no actual bid set. Service
    # doesn't validate this today; the client will ignore it server-side.
    # Pinning so we notice if that becomes a service-level error later.
    await BiddingService(settings).apply([BidUpdate(keyword_id=1)], _applying_plan_id="test-bypass")

    [[bid]] = fake_direct.set_keyword_bids_calls
    assert bid.keyword_id == 1
    assert bid.search_bid is None
    assert bid.network_bid is None


# --------------------------------------------------------------------------
# apply(): batching.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_batches_multiple_updates_into_one_call(
    settings: Settings, fake_direct: _FakeDirectService
) -> None:
    # The service sends all updates to the API in a single request — we
    # do not want one keyword per HTTP call (that would burn Units quota
    # and make the audit trail illegible).
    updates = [
        BidUpdate(keyword_id=1, new_search_bid_rub=1.0),
        BidUpdate(keyword_id=2, new_search_bid_rub=2.0),
        BidUpdate(keyword_id=3, new_network_bid_rub=3.0),
    ]

    await BiddingService(settings).apply(updates, _applying_plan_id="test-bypass")

    assert len(fake_direct.set_keyword_bids_calls) == 1
    batch = fake_direct.set_keyword_bids_calls[0]
    assert [b.keyword_id for b in batch] == [1, 2, 3]
    assert batch[0].search_bid == 1_000_000
    assert batch[1].search_bid == 2_000_000
    assert batch[2].network_bid == 3_000_000


# --------------------------------------------------------------------------
# @requires_plan gating (M2 follow-up).
# --------------------------------------------------------------------------


class _CapturingSink:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def emit(self, event: Any) -> None:
        self.events.append(event)


@pytest.mark.asyncio
async def test_apply_without_safety_pair_raises_runtime_error(
    settings: Settings,
) -> None:
    """Mutating ``apply()`` without ``pipeline``/``store`` and without
    the bypass kwarg must fail loudly. Silent fallback would let an
    agent set arbitrary bids without safety review.
    """
    svc = BiddingService(settings)
    with pytest.raises(RuntimeError, match="SafetyPipeline"):
        await svc.apply([BidUpdate(keyword_id=1, new_search_bid_rub=10.0)])


@pytest.mark.asyncio
async def test_apply_through_decorator_persists_confirm_plan(
    settings: Settings, fake_direct: _FakeDirectService, tmp_path: Any
) -> None:
    """End-to-end: with no auto_approve_bid_change knob, every bid
    change returns confirm → plan persisted → operator must run
    apply-plan to actually mutate. ``DirectService.set_keyword_bids``
    is NOT called.
    """
    from yadirect_agent.agent.executor import PlanRequired
    from yadirect_agent.agent.pipeline import SafetyPipeline
    from yadirect_agent.agent.plans import PendingPlansStore
    from yadirect_agent.agent.safety import (
        BudgetCapPolicy,
        ConversionIntegrityPolicy,
        MaxCpcPolicy,
        Policy,
        QueryDriftPolicy,
    )

    policy = Policy(
        budget_cap=BudgetCapPolicy(account_daily_budget_cap_rub=100_000),
        max_cpc=MaxCpcPolicy(),
        query_drift=QueryDriftPolicy(),
        conversion_integrity=ConversionIntegrityPolicy(
            min_conversions_total=1,
            min_ratio_vs_baseline=0.5,
            require_all_baseline_goals_present=True,
        ),
        rollout_stage="autonomy_full",
    )
    pipeline = SafetyPipeline(policy)
    store = PendingPlansStore(tmp_path / "pending_plans.jsonl")
    sink = _CapturingSink()
    svc = BiddingService(settings, pipeline=pipeline, store=store, audit_sink=sink)

    with pytest.raises(PlanRequired) as exc:
        await svc.apply(
            [BidUpdate(keyword_id=42, new_search_bid_rub=10.0)],
        )

    plan = store.get(exc.value.plan_id)
    assert plan is not None
    assert plan.status == "pending"
    assert plan.action == "set_keyword_bids"
    assert plan.resource_ids == [42]
    # Args round-trip-able through JSON because BidUpdate is now
    # pydantic, not a dataclass.
    assert plan.args["updates"][0]["keyword_id"] == 42
    assert plan.args["updates"][0]["new_search_bid_rub"] == 10.0
    # DirectService NOT called.
    assert fake_direct.set_keyword_bids_calls == []
