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
from yadirect_agent.models.keywords import Keyword, KeywordBid
from yadirect_agent.services.bidding import BiddingService, BidUpdate, _build_bid_context

# --------------------------------------------------------------------------
# In-memory DirectService stub.
# --------------------------------------------------------------------------


class _FakeDirectService:
    """Captures the payload that reaches set_keyword_bids and serves
    canned ``get_keywords`` responses for the bid-context reader."""

    def __init__(self) -> None:
        self.set_keyword_bids_calls: list[list[KeywordBid]] = []
        self.get_keywords_calls: list[dict[str, Any]] = []
        self.keywords_to_return: list[Keyword] = []

    async def __aenter__(self) -> _FakeDirectService:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    async def set_keyword_bids(self, bids: list[KeywordBid]) -> dict[str, Any]:
        # Copy so later mutations in the service don't rewrite captures.
        self.set_keyword_bids_calls.append(list(bids))
        return {}

    async def get_keywords(
        self,
        adgroup_ids: list[int] | None = None,
        *,
        keyword_ids: list[int] | None = None,
        limit: int = 10_000,
    ) -> list[Keyword]:
        self.get_keywords_calls.append(
            {"adgroup_ids": adgroup_ids, "keyword_ids": keyword_ids, "limit": limit}
        )
        return list(self.keywords_to_return)


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


@pytest.mark.asyncio
async def test_apply_rollout_stage_shadow_rejects_bid_changes(
    settings: Settings, fake_direct: _FakeDirectService, tmp_path: Any
) -> None:
    """Auditor M2-bidding M-3: a bid change on rollout_stage="shadow"
    must surface as PlanRejected via the rollout-stage allow-list.
    Pins the contract that operators on shadow see every bid attempt
    blocked at plan-creation time.
    """
    from yadirect_agent.agent.executor import PlanRejected
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
        rollout_stage="shadow",  # read-only stage
    )
    pipeline = SafetyPipeline(policy)
    store = PendingPlansStore(tmp_path / "pending_plans.jsonl")
    sink = _CapturingSink()
    svc = BiddingService(settings, pipeline=pipeline, store=store, audit_sink=sink)

    with pytest.raises(PlanRejected):
        await svc.apply([BidUpdate(keyword_id=42, new_search_bid_rub=10.0)])

    # No mutating call reached DirectService.
    assert fake_direct.set_keyword_bids_calls == []


@pytest.mark.asyncio
async def test_resolve_safety_requires_audit_sink(settings: Settings, tmp_path: Any) -> None:
    """Auditor M2-bidding C-1: a service constructed with pipeline+store
    but no audit_sink must refuse the non-bypass path. Otherwise a
    misconfigured production caller gets gated plans but no audit
    emission — a silent gap.
    """
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
    svc = BiddingService(settings, pipeline=pipeline, store=store)
    # No audit_sink. Mutating call without bypass → RuntimeError.
    with pytest.raises(RuntimeError, match="AuditSink"):
        await svc.apply([BidUpdate(keyword_id=1, new_search_bid_rub=5.0)])


# --------------------------------------------------------------------------
# _build_bid_context: AccountBidSnapshot reader (M2 follow-up).
#
# Until this PR, ``_build_bid_context`` returned an empty
# ``AccountBidSnapshot`` and KS#2 / KS#4 silently deferred on every
# bid call. These tests pin the new contract: the builder must read
# per-keyword bid + productivity from Direct via ``get_keywords`` and
# populate ``KeywordSnapshot`` so the safety pipeline can actually
# enforce max-CPC and quality-score thresholds.
# --------------------------------------------------------------------------


def _build_policy_for_bidding(
    *,
    rollout_stage: str = "autonomy_full",
    campaign_max_cpc_rub: dict[int, float] | None = None,
    min_quality_score: int = 5,
) -> Any:
    """Helper assembling the minimal Policy a BiddingService needs.

    KS#2 (max-CPC) reads ``campaign_max_cpc_rub``;
    KS#4 reads ``min_quality_score_for_bid_increase``. The other
    slices stay at their non-blocking defaults.
    """
    from yadirect_agent.agent.safety import (
        BudgetCapPolicy,
        ConversionIntegrityPolicy,
        MaxCpcPolicy,
        Policy,
        QualityScoreGuardPolicy,
        QueryDriftPolicy,
    )

    return Policy(
        budget_cap=BudgetCapPolicy(account_daily_budget_cap_rub=100_000),
        max_cpc=MaxCpcPolicy(campaign_max_cpc_rub=campaign_max_cpc_rub or {}),
        quality_score_guard=QualityScoreGuardPolicy(
            min_quality_score_for_bid_increase=min_quality_score
        ),
        query_drift=QueryDriftPolicy(),
        conversion_integrity=ConversionIntegrityPolicy(
            min_conversions_total=1,
            min_ratio_vs_baseline=0.5,
            require_all_baseline_goals_present=True,
        ),
        rollout_stage=rollout_stage,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_build_bid_context_with_no_updates_skips_api_call(
    settings: Settings, fake_direct: _FakeDirectService
) -> None:
    """Empty updates → empty snapshot, no network round-trip. Saves
    a Units charge in the (degenerate but harmless) edge case where
    a caller invokes apply with [] and the @requires_plan decorator
    runs the context builder before the empty short-circuit fires."""
    svc = BiddingService(settings)

    ctx = await _build_bid_context(svc, [])

    assert ctx.bid_snapshot is not None
    assert ctx.bid_snapshot.keywords == []
    assert fake_direct.get_keywords_calls == []


@pytest.mark.asyncio
async def test_build_bid_context_fetches_by_keyword_ids(
    settings: Settings, fake_direct: _FakeDirectService
) -> None:
    """The builder must call ``get_keywords`` with exactly the
    keyword_ids from the updates list — not by adgroup, not by
    everything-on-the-account."""
    svc = BiddingService(settings)

    await _build_bid_context(
        svc,
        [
            BidUpdate(keyword_id=42, new_search_bid_rub=10.0),
            BidUpdate(keyword_id=99, new_network_bid_rub=2.5),
        ],
    )

    [call] = fake_direct.get_keywords_calls
    assert call["keyword_ids"] == [42, 99]
    assert call["adgroup_ids"] is None


@pytest.mark.asyncio
async def test_build_bid_context_populates_snapshot_from_api_rows(
    settings: Settings, fake_direct: _FakeDirectService
) -> None:
    """End-to-end-of-the-builder: API row → KeywordSnapshot with the
    fields KS#2 / KS#4 actually read."""
    fake_direct.keywords_to_return = [
        Keyword.model_validate(
            {
                "Id": 42,
                "AdGroupId": 100,
                "CampaignId": 7,
                "Keyword": "k",
                "Bid": 8_000_000,  # 8 RUB
                "ContextBid": 2_000_000,  # 2 RUB
                "Productivity": {"Value": 9},
            }
        )
    ]
    svc = BiddingService(settings)

    ctx = await _build_bid_context(svc, [BidUpdate(keyword_id=42, new_search_bid_rub=10.0)])

    assert ctx.bid_snapshot is not None
    [snap] = ctx.bid_snapshot.keywords
    assert snap.keyword_id == 42
    assert snap.campaign_id == 7
    assert snap.current_search_bid_rub == 8.0
    assert snap.current_network_bid_rub == 2.0
    assert snap.quality_score == 9


@pytest.mark.asyncio
async def test_build_bid_context_records_baseline_timestamp(
    settings: Settings, fake_direct: _FakeDirectService
) -> None:
    """The bid context must stamp ``baseline_timestamp`` so the audit
    sink (M2.3) and the apply-plan re-review path can detect a stale
    snapshot. KS#4's ``_is_increase`` compares the proposed bid
    against the snapshot's ``current_*_bid_rub`` — an undated,
    arbitrarily-old snapshot that survives apply-plan re-review
    would hide a parallel-operator bid bump and let a second
    consecutive increase slip past KS#4 unnoticed (auditor
    M2-bid-snapshot HIGH-2)."""
    from datetime import UTC, datetime, timedelta

    fake_direct.keywords_to_return = [
        Keyword.model_validate(
            {
                "Id": 42,
                "AdGroupId": 100,
                "CampaignId": 7,
                "Keyword": "k",
                "Bid": 5_000_000,
                "Productivity": {"Value": 9},
            }
        )
    ]
    svc = BiddingService(settings)

    before = datetime.now(UTC)
    ctx = await _build_bid_context(svc, [BidUpdate(keyword_id=42, new_search_bid_rub=10.0)])
    after = datetime.now(UTC)

    assert ctx.baseline_timestamp is not None
    assert ctx.baseline_timestamp.tzinfo is not None  # tz-aware, not naive
    # Generous bracket — the call is fast but the test must not be
    # flaky on a busy CI host.
    assert before - timedelta(seconds=1) <= ctx.baseline_timestamp <= after + timedelta(seconds=1)


@pytest.mark.asyncio
async def test_build_bid_context_skips_rows_with_missing_identifiers(
    settings: Settings, fake_direct: _FakeDirectService
) -> None:
    """Defensive: a row from Direct missing ``Id`` or ``CampaignId``
    cannot become a meaningful KeywordSnapshot (KS#2 looks up by
    campaign, KS#4 by keyword id). Skip rather than crash —
    snapshot stays partial and any unknown-keyword update routes
    through the existing "snapshot.find returns None → defer" branch
    in MaxCpcCheck / QualityScoreGuardCheck.
    """
    fake_direct.keywords_to_return = [
        Keyword.model_validate({"Id": 1, "AdGroupId": 100, "Keyword": "no-campaign"}),
        Keyword.model_validate({"AdGroupId": 100, "CampaignId": 7, "Keyword": "no-id"}),
        Keyword.model_validate({"Id": 2, "AdGroupId": 100, "CampaignId": 7, "Keyword": "ok"}),
    ]
    svc = BiddingService(settings)

    ctx = await _build_bid_context(svc, [BidUpdate(keyword_id=2, new_search_bid_rub=5.0)])

    assert ctx.bid_snapshot is not None
    [snap] = ctx.bid_snapshot.keywords
    assert snap.keyword_id == 2


# --------------------------------------------------------------------------
# End-to-end: KS#2 max-CPC + KS#4 QS guardrail now actually fire.
#
# Pre-PR these checks deferred because the snapshot was empty. With
# the snapshot reader wired, both kill-switches see real per-keyword
# state and reject the violating plan. Pinned so a future regression
# in the snapshot path doesn't silently restore the deferred-on-empty
# behaviour without a red test.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_blocks_when_search_bid_exceeds_campaign_max_cpc(
    settings: Settings, fake_direct: _FakeDirectService, tmp_path: Any
) -> None:
    """KS#2: a search bid above the campaign's configured max CPC must
    surface as ``PlanRejected``. Before this PR the empty snapshot
    let the bid through (deferred check); the test would fail by
    raising ``PlanRequired`` instead."""
    from yadirect_agent.agent.executor import PlanRejected
    from yadirect_agent.agent.pipeline import SafetyPipeline
    from yadirect_agent.agent.plans import PendingPlansStore

    fake_direct.keywords_to_return = [
        Keyword.model_validate(
            {
                "Id": 42,
                "AdGroupId": 100,
                "CampaignId": 7,
                "Keyword": "k",
                "Bid": 5_000_000,
                "Productivity": {"Value": 9},
            }
        )
    ]
    policy = _build_policy_for_bidding(campaign_max_cpc_rub={7: 10.0})
    pipeline = SafetyPipeline(policy)
    store = PendingPlansStore(tmp_path / "pending_plans.jsonl")
    sink = _CapturingSink()
    svc = BiddingService(settings, pipeline=pipeline, store=store, audit_sink=sink)

    # Bid 15 RUB > campaign cap 10 RUB on campaign 7 → KS#2 blocks.
    with pytest.raises(PlanRejected):
        await svc.apply([BidUpdate(keyword_id=42, new_search_bid_rub=15.0)])

    # No mutating call reached DirectService.
    assert fake_direct.set_keyword_bids_calls == []


@pytest.mark.asyncio
async def test_apply_blocks_bid_increase_on_low_quality_score(
    settings: Settings, fake_direct: _FakeDirectService, tmp_path: Any
) -> None:
    """KS#4: an increase on a keyword whose QS is below the
    configured floor is rejected. Before this PR the empty snapshot
    meant ``KeywordSnapshot.quality_score`` was always None → KS#4
    deferred. Now the real snapshot carries QS=3, the increase from
    5→8 RUB trips the guard, and the call surfaces as ``PlanRejected``."""
    from yadirect_agent.agent.executor import PlanRejected
    from yadirect_agent.agent.pipeline import SafetyPipeline
    from yadirect_agent.agent.plans import PendingPlansStore

    fake_direct.keywords_to_return = [
        Keyword.model_validate(
            {
                "Id": 42,
                "AdGroupId": 100,
                "CampaignId": 7,
                "Keyword": "k",
                "Bid": 5_000_000,  # 5 RUB
                "Productivity": {"Value": 3},  # below default floor of 5
            }
        )
    ]
    policy = _build_policy_for_bidding(min_quality_score=5)
    pipeline = SafetyPipeline(policy)
    store = PendingPlansStore(tmp_path / "pending_plans.jsonl")
    sink = _CapturingSink()
    svc = BiddingService(settings, pipeline=pipeline, store=store, audit_sink=sink)

    with pytest.raises(PlanRejected):
        await svc.apply([BidUpdate(keyword_id=42, new_search_bid_rub=8.0)])

    assert fake_direct.set_keyword_bids_calls == []


@pytest.mark.asyncio
async def test_apply_allows_bid_decrease_on_low_quality_score(
    settings: Settings, fake_direct: _FakeDirectService, tmp_path: Any
) -> None:
    """KS#4 only blocks INCREASES on low-QS keywords. A decrease is
    the operator doing the right thing — let it through to the
    confirm path. This pins the asymmetry so a future refactor
    doesn't mistake "bid touched" for "bid raised"."""
    from yadirect_agent.agent.executor import PlanRequired
    from yadirect_agent.agent.pipeline import SafetyPipeline
    from yadirect_agent.agent.plans import PendingPlansStore

    fake_direct.keywords_to_return = [
        Keyword.model_validate(
            {
                "Id": 42,
                "AdGroupId": 100,
                "CampaignId": 7,
                "Keyword": "k",
                "Bid": 10_000_000,  # 10 RUB
                "Productivity": {"Value": 3},
            }
        )
    ]
    policy = _build_policy_for_bidding(min_quality_score=5)
    pipeline = SafetyPipeline(policy)
    store = PendingPlansStore(tmp_path / "pending_plans.jsonl")
    sink = _CapturingSink()
    svc = BiddingService(settings, pipeline=pipeline, store=store, audit_sink=sink)

    # 5 < 10 → decrease, KS#4 must NOT block. Pipeline returns confirm
    # because there's no auto_approve_bid_change knob.
    with pytest.raises(PlanRequired):
        await svc.apply([BidUpdate(keyword_id=42, new_search_bid_rub=5.0)])

    assert fake_direct.set_keyword_bids_calls == []
