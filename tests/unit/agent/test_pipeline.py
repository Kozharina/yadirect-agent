"""Tests for SafetyPipeline, the orchestrator that aggregates the seven
kill-switches into a single allow / confirm / reject decision.

Structure:
- TestForbiddenOperations: policy.forbidden_operations rejects at call site.
- TestRolloutStage: allowed-action sets per stage.
- TestReadOnlyShortCircuit: read-only actions skip all checks.
- TestSystemGatekeepers: KS#6 / KS#7 block the whole plan.
- TestPerOperationChecks: KS#1/#2/#3/#4/#5 dispatch.
- TestApprovalTiers: confirm for non-auto-approved actions.
- TestSessionTOCTOU: cross-call bid ratcheting refused.
- TestSkippedChecks: missing snapshots → skip, not fail.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

from pydantic import ValidationError

from yadirect_agent.agent.pipeline import (
    ReviewContext,
    SafetyDecision,
    SafetyPipeline,
    SessionState,
)
from yadirect_agent.agent.plans import OperationPlan
from yadirect_agent.agent.safety import (
    AccountBidSnapshot,
    AccountBudgetSnapshot,
    BudgetCapPolicy,
    BudgetChange,
    CampaignBudget,
    ConversionIntegrityPolicy,
    ConversionsSnapshot,
    GoalConversions,
    KeywordSnapshot,
    MaxCpcPolicy,
    Policy,
    ProposedBidChange,
    QueryDriftPolicy,
    RolloutStage,
    SearchQueriesSnapshot,
)

# --------------------------------------------------------------------------
# Helpers.
# --------------------------------------------------------------------------


def _policy(
    *,
    rollout_stage: RolloutStage = "autonomy_full",
    account_cap: int = 100_000,
    auto_approve_resume: bool = True,
    auto_approve_pause: bool = True,
    auto_approve_negative_keywords: bool = True,
    forbidden: list[str] | None = None,
    max_cpc_by_campaign: dict[int, float] | None = None,
    query_drift_max_share: float = 0.4,
    conversion_min_total: int = 1,
) -> Policy:
    return Policy(
        budget_cap=BudgetCapPolicy(account_daily_budget_cap_rub=account_cap),
        max_cpc=MaxCpcPolicy(campaign_max_cpc_rub=max_cpc_by_campaign or {}),
        query_drift=QueryDriftPolicy(max_new_query_share=query_drift_max_share),
        conversion_integrity=ConversionIntegrityPolicy(
            min_conversions_total=conversion_min_total,
            min_ratio_vs_baseline=0.5,
            require_all_baseline_goals_present=True,
        ),
        rollout_stage=rollout_stage,
        auto_approve_resume=auto_approve_resume,
        auto_approve_pause=auto_approve_pause,
        auto_approve_negative_keywords=auto_approve_negative_keywords,
        forbidden_operations=forbidden
        if forbidden is not None
        else ["delete_campaigns", "delete_ads", "archive_campaigns_bulk"],
    )


def _plan(
    action: str,
    *,
    plan_id: str = "p1",
    resource_ids: list[int] | None = None,
) -> OperationPlan:
    return OperationPlan(
        plan_id=plan_id,
        created_at=datetime.now(UTC),
        action=action,
        resource_type="campaign",
        resource_ids=resource_ids or [1],
        args={},
        preview=f"{action} for preview",
        reason="unit test",
    )


def _ctx(
    *,
    budget_snapshot: AccountBudgetSnapshot | None = None,
    budget_changes: list[BudgetChange] | None = None,
    bid_snapshot: AccountBidSnapshot | None = None,
    bid_changes: list[ProposedBidChange] | None = None,
    budget_baseline: AccountBudgetSnapshot | None = None,
    conversions_baseline: ConversionsSnapshot | None = None,
    conversions_current: ConversionsSnapshot | None = None,
    queries_baseline: SearchQueriesSnapshot | None = None,
    queries_current: SearchQueriesSnapshot | None = None,
) -> ReviewContext:
    return ReviewContext(
        budget_snapshot=budget_snapshot,
        budget_changes=budget_changes or [],
        bid_snapshot=bid_snapshot,
        bid_changes=bid_changes or [],
        budget_baseline=budget_baseline,
        conversions_baseline=conversions_baseline,
        conversions_current=conversions_current,
        queries_baseline=queries_baseline,
        queries_current=queries_current,
    )


# --------------------------------------------------------------------------
# Forbidden operations.
# --------------------------------------------------------------------------


class TestForbiddenOperations:
    def test_rejects_default_forbidden_action(self) -> None:
        pipe = SafetyPipeline(_policy())
        result = pipe.review(_plan("delete_campaigns"), _ctx())
        assert result.status == "reject"
        assert "forbidden_operations" in result.reason

    def test_normalisation_catches_case_drift(self) -> None:
        # Operator's policy normalised lowercase at policy load.
        # Agent calls with CamelCase action name → pipeline
        # normalises and matches anyway.
        pipe = SafetyPipeline(_policy())
        result = pipe.review(_plan("Delete_Campaigns"), _ctx())
        assert result.status == "reject"

    def test_normalisation_catches_whitespace(self) -> None:
        pipe = SafetyPipeline(_policy())
        result = pipe.review(_plan(" delete_ads "), _ctx())
        assert result.status == "reject"

    def test_custom_forbidden_list_takes_effect(self) -> None:
        pipe = SafetyPipeline(_policy(forbidden=["pause_campaigns"]))
        result = pipe.review(_plan("pause_campaigns"), _ctx())
        assert result.status == "reject"


# --------------------------------------------------------------------------
# Rollout stage.
# --------------------------------------------------------------------------


class TestRolloutStage:
    def test_shadow_allows_read_only(self) -> None:
        pipe = SafetyPipeline(_policy(rollout_stage="shadow"))
        result = pipe.review(_plan("list_campaigns"), _ctx())
        assert result.status == "allow"

    def test_shadow_rejects_mutating_action(self) -> None:
        pipe = SafetyPipeline(_policy(rollout_stage="shadow"))
        result = pipe.review(_plan("pause_campaigns"), _ctx())
        assert result.status == "reject"
        assert "shadow" in result.reason

    def test_assist_allows_pause_but_rejects_budget(self) -> None:
        pipe = SafetyPipeline(_policy(rollout_stage="assist"))
        assert pipe.review(_plan("pause_campaigns"), _ctx()).status == "allow"
        assert pipe.review(_plan("set_campaign_budget"), _ctx()).status == "reject"

    def test_autonomy_light_allows_budget_edits(self) -> None:
        pipe = SafetyPipeline(_policy(rollout_stage="autonomy_light"))
        result = pipe.review(_plan("set_campaign_budget"), _ctx())
        assert result.status == "allow"

    def test_autonomy_full_allows_create_campaign(self) -> None:
        pipe = SafetyPipeline(_policy(rollout_stage="autonomy_full"))
        result = pipe.review(_plan("create_campaign"), _ctx())
        assert result.status == "allow"

    def test_unknown_action_rejected_even_in_autonomy_full(self) -> None:
        # Defence in depth — a completely unknown action string can't
        # slip through just because the stage is permissive.
        pipe = SafetyPipeline(_policy(rollout_stage="autonomy_full"))
        result = pipe.review(_plan("totally_new_operation"), _ctx())
        assert result.status == "reject"


# --------------------------------------------------------------------------
# Read-only short-circuit.
# --------------------------------------------------------------------------


class TestReadOnlyShortCircuit:
    def test_read_only_skips_all_checks(self) -> None:
        # Even with blocking snapshots present, a read-only action
        # shouldn't run the checks at all.
        pipe = SafetyPipeline(_policy())
        # Empty conversions would normally warn.
        result = pipe.review(
            _plan("list_campaigns"),
            _ctx(
                conversions_baseline=ConversionsSnapshot(counter_id=1, goals=[]),
                conversions_current=ConversionsSnapshot(counter_id=1, goals=[]),
            ),
        )
        assert result.status == "allow"
        assert result.warnings == []
        assert result.skipped_checks == []


# --------------------------------------------------------------------------
# System-level gatekeepers.
# --------------------------------------------------------------------------


class TestSystemGatekeepers:
    def test_conversion_collapse_blocks_mutating_plan(self) -> None:
        pipe = SafetyPipeline(_policy(conversion_min_total=10))
        result = pipe.review(
            _plan("pause_campaigns"),
            _ctx(
                conversions_baseline=ConversionsSnapshot(
                    counter_id=1,
                    goals=[GoalConversions(goal_id=1, goal_name="g", conversions=100)],
                ),
                # Current = 0 → below absolute floor of 10.
                conversions_current=ConversionsSnapshot(counter_id=1, goals=[]),
            ),
        )
        assert result.status == "reject"
        assert any(cr.reason and "minimum" in cr.reason for cr in result.blocking_checks)

    def test_query_drift_blocks_mutating_plan(self) -> None:
        pipe = SafetyPipeline(_policy(query_drift_max_share=0.1))
        result = pipe.review(
            _plan("pause_campaigns"),
            _ctx(
                queries_baseline=SearchQueriesSnapshot(counter_id=1, queries=["a", "b"]),
                queries_current=SearchQueriesSnapshot(counter_id=1, queries=["a", "x", "y", "z"]),
            ),
        )
        assert result.status == "reject"
        assert any(
            "drift" in (cr.reason or "").lower() or "exceeds" in (cr.reason or "").lower()
            for cr in result.blocking_checks
        )


# --------------------------------------------------------------------------
# Per-operation checks.
# --------------------------------------------------------------------------


class TestPerOperationChecks:
    def test_budget_cap_blocks_over_cap_plan(self) -> None:
        pipe = SafetyPipeline(_policy(account_cap=10_000))
        snap = AccountBudgetSnapshot(
            campaigns=[CampaignBudget(id=1, name="c1", daily_budget_rub=5_000, state="ON")]
        )
        result = pipe.review(
            _plan("set_campaign_budget"),
            _ctx(
                budget_snapshot=snap,
                budget_changes=[BudgetChange(campaign_id=1, new_daily_budget_rub=15_000)],
            ),
        )
        assert result.status == "reject"

    def test_budget_cap_passes_under_cap(self) -> None:
        pipe = SafetyPipeline(_policy(account_cap=10_000))
        snap = AccountBudgetSnapshot(
            campaigns=[CampaignBudget(id=1, name="c1", daily_budget_rub=5_000, state="ON")]
        )
        result = pipe.review(
            _plan("set_campaign_budget"),
            _ctx(
                budget_snapshot=snap,
                budget_changes=[BudgetChange(campaign_id=1, new_daily_budget_rub=7_000)],
            ),
        )
        assert result.status == "allow"

    def test_max_cpc_blocks_over_cap_bid(self) -> None:
        pipe = SafetyPipeline(_policy(max_cpc_by_campaign={100: 20.0}))
        snap = AccountBidSnapshot(
            keywords=[
                KeywordSnapshot(
                    keyword_id=1,
                    campaign_id=100,
                    current_search_bid_rub=10.0,
                    quality_score=7,
                )
            ]
        )
        result = pipe.review(
            _plan("set_keyword_bids"),
            _ctx(
                bid_snapshot=snap,
                bid_changes=[ProposedBidChange(keyword_id=1, new_search_bid_rub=25.0)],
            ),
        )
        assert result.status == "reject"


# --------------------------------------------------------------------------
# Approval tiers.
# --------------------------------------------------------------------------


class TestApprovalTiers:
    def test_resume_requires_confirmation_when_flag_off(self) -> None:
        pipe = SafetyPipeline(_policy(auto_approve_resume=False))
        result = pipe.review(_plan("resume_campaigns"), _ctx())
        assert result.status == "confirm"
        assert "resume" in result.reason

    def test_resume_allowed_when_flag_on(self) -> None:
        pipe = SafetyPipeline(_policy(auto_approve_resume=True))
        result = pipe.review(_plan("resume_campaigns"), _ctx())
        assert result.status == "allow"

    def test_pause_auto_approved_by_default(self) -> None:
        pipe = SafetyPipeline(_policy())
        result = pipe.review(_plan("pause_campaigns"), _ctx())
        assert result.status == "allow"

    def test_pause_requires_confirmation_when_flag_off(self) -> None:
        pipe = SafetyPipeline(_policy(auto_approve_pause=False))
        result = pipe.review(_plan("pause_campaigns"), _ctx())
        assert result.status == "confirm"

    def test_negative_keywords_require_confirmation_when_flag_off(self) -> None:
        pipe = SafetyPipeline(_policy(auto_approve_negative_keywords=False))
        result = pipe.review(_plan("add_negative_keywords"), _ctx())
        assert result.status == "confirm"


# --------------------------------------------------------------------------
# Session TOCTOU (cross-call bid ratcheting).
# --------------------------------------------------------------------------


class TestSessionTOCTOU:
    def test_second_call_higher_than_first_approved_bid_is_rejected(self) -> None:
        # First call: propose bid=10.0 against snapshot where current=8.0 →
        # approved. Pipeline records max_approved_bid_per_keyword[1]=10.0.
        # Second call: propose bid=12.0 against a fresh snapshot where
        # current=10.0 (i.e. already applied). Per-snapshot max_cpc
        # check would pass (no cap configured), but session TOCTOU
        # sees 12.0 > 10.0 and blocks.
        session = SessionState()
        pipe = SafetyPipeline(_policy(), session_state=session)
        snap1 = AccountBidSnapshot(
            keywords=[
                KeywordSnapshot(
                    keyword_id=1,
                    campaign_id=100,
                    current_search_bid_rub=8.0,
                )
            ]
        )

        first = pipe.review(
            _plan("set_keyword_bids", plan_id="p1"),
            _ctx(
                bid_snapshot=snap1,
                bid_changes=[ProposedBidChange(keyword_id=1, new_search_bid_rub=10.0)],
            ),
        )
        assert first.status == "allow"
        assert session.approved_bid_ceiling(1) == 10.0

        snap2 = AccountBidSnapshot(
            keywords=[
                KeywordSnapshot(
                    keyword_id=1,
                    campaign_id=100,
                    current_search_bid_rub=10.0,  # reflects the first call
                )
            ]
        )
        second = pipe.review(
            _plan("set_keyword_bids", plan_id="p2"),
            _ctx(
                bid_snapshot=snap2,
                bid_changes=[ProposedBidChange(keyword_id=1, new_search_bid_rub=12.0)],
            ),
        )
        assert second.status == "reject"
        assert any("session-approved" in (cr.reason or "") for cr in second.blocking_checks)

    def test_lowering_bid_never_trips_toctou(self) -> None:
        session = SessionState()
        pipe = SafetyPipeline(_policy(), session_state=session)
        snap = AccountBidSnapshot(
            keywords=[
                KeywordSnapshot(
                    keyword_id=1,
                    campaign_id=100,
                    current_search_bid_rub=5.0,
                )
            ]
        )
        pipe.review(
            _plan("set_keyword_bids", plan_id="p1"),
            _ctx(
                bid_snapshot=snap,
                bid_changes=[ProposedBidChange(keyword_id=1, new_search_bid_rub=10.0)],
            ),
        )
        # Then lower.
        result = pipe.review(
            _plan("set_keyword_bids", plan_id="p2"),
            _ctx(
                bid_snapshot=snap,
                bid_changes=[ProposedBidChange(keyword_id=1, new_search_bid_rub=3.0)],
            ),
        )
        assert result.status == "allow"


# --------------------------------------------------------------------------
# Skipped checks.
# --------------------------------------------------------------------------


class TestSkippedChecks:
    def test_missing_budget_snapshot_skips_budget_cap_check(self) -> None:
        pipe = SafetyPipeline(_policy())
        result = pipe.review(
            _plan("set_campaign_budget"),
            _ctx(budget_snapshot=None),
        )
        assert result.status == "allow"
        assert "budget_cap" in result.skipped_checks

    def test_missing_baselines_skip_temporal_checks(self) -> None:
        pipe = SafetyPipeline(_policy())
        result = pipe.review(_plan("pause_campaigns"), _ctx())
        assert result.status == "allow"
        assert "conversion_integrity" in result.skipped_checks
        assert "query_drift" in result.skipped_checks


# --------------------------------------------------------------------------
# Decision shape.
# --------------------------------------------------------------------------


class TestDecisionShape:
    def test_allow_properties(self) -> None:
        d = SafetyDecision(status="allow", reason="ok")
        assert d.allowed is True
        assert d.requires_confirmation is False

    def test_confirm_properties(self) -> None:
        d = SafetyDecision(status="confirm", reason="human needed")
        assert d.allowed is False
        assert d.requires_confirmation is True

    def test_reject_properties(self) -> None:
        d = SafetyDecision(status="reject", reason="blocked")
        assert d.allowed is False
        assert d.requires_confirmation is False


# --------------------------------------------------------------------------
# Session state.
# --------------------------------------------------------------------------


class TestSessionState:
    def test_record_approved_keeps_max(self) -> None:
        s = SessionState()
        s.record_approved_bid(1, 10.0)
        s.record_approved_bid(1, 15.0)
        s.record_approved_bid(1, 8.0)  # lower — doesn't overwrite
        assert s.approved_bid_ceiling(1) == 15.0

    def test_approved_bid_ceiling_none_when_unknown(self) -> None:
        assert SessionState().approved_bid_ceiling(1) is None


# Silence "unused import" mypy check on cast / ValidationError — we
# keep them in the imports for future test expansion.
_ = cast
_ = ValidationError
