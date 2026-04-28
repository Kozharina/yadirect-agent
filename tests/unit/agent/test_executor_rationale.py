"""Tests for rationale emission in @requires_plan (M20.2).

Scope:
- ``rationale=`` kwarg flows through @requires_plan to RationaleStore;
- decision_id is overwritten with plan_id so caller-provided ids
  cannot diverge from the plan they describe;
- emission happens on ``allow`` and ``confirm`` paths, NOT on
  ``reject`` (rejected plans never had a decision to act on);
- emission happens BEFORE the wrapped method runs (so a method
  that raises still leaves the rationale on disk);
- soft-optional today: rationale missing ⇒ structlog warning,
  operation continues; rationale present but no rationale store ⇒
  structlog warning, operation continues.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from yadirect_agent.agent.executor import requires_plan
from yadirect_agent.agent.pipeline import ReviewContext, SafetyDecision
from yadirect_agent.agent.plans import OperationPlan, PendingPlansStore
from yadirect_agent.agent.rationale_store import RationaleStore
from yadirect_agent.agent.safety import (
    AccountBudgetSnapshot,
    BudgetChange,
    CampaignBudget,
)
from yadirect_agent.models.rationale import (
    Confidence,
    InputDataPoint,
    Rationale,
)


# --------------------------------------------------------------------------
# Stubs (parallel to test_executor.py — kept local for isolation).
# --------------------------------------------------------------------------


@dataclass
class _StubPolicy:
    max_snapshot_age_seconds: int = 86_400


@dataclass
class _StubPipeline:
    next_decision: SafetyDecision = field(
        default_factory=lambda: SafetyDecision(status="allow", reason="ok"),
    )
    review_calls: list[tuple[OperationPlan, ReviewContext]] = field(default_factory=list)
    on_applied_calls: list[ReviewContext] = field(default_factory=list)
    policy: _StubPolicy = field(default_factory=_StubPolicy)

    def review(self, plan: OperationPlan, context: ReviewContext) -> SafetyDecision:
        self.review_calls.append((plan, context))
        return self.next_decision

    def on_applied(self, context: ReviewContext) -> None:
        self.on_applied_calls.append(context)


def _ctx() -> ReviewContext:
    snap = AccountBudgetSnapshot(
        campaigns=[CampaignBudget(id=1, name="c", daily_budget_rub=100, state="ON")],
    )
    return ReviewContext(
        budget_snapshot=snap,
        budget_changes=[BudgetChange(campaign_id=1, new_daily_budget_rub=150)],
    )


async def _async_ctx_builder(self: Any, campaign_id: int, new_budget_rub: int) -> ReviewContext:
    return _ctx()


class _FakeServiceWithRationale:
    """Service that exposes both safety and rationale stores."""

    def __init__(
        self,
        pipeline: _StubPipeline,
        store: PendingPlansStore,
        rationale_store: RationaleStore | None,
    ) -> None:
        self._pipeline = pipeline
        self._plans_store = store
        self._rationale_store = rationale_store
        self.calls: list[dict[str, Any]] = []

    def _resolve_safety(self) -> tuple[_StubPipeline, PendingPlansStore]:
        return self._pipeline, self._plans_store

    def _resolve_rationale_store(self) -> RationaleStore | None:
        return self._rationale_store

    @requires_plan(
        action="set_campaign_budget",
        resource_type="campaign",
        preview_builder=lambda self, campaign_id, new_budget_rub: (
            f"set budget on campaign {campaign_id} to {new_budget_rub} RUB"
        ),
        context_builder=_async_ctx_builder,
        resource_ids_from_args=lambda self, campaign_id, new_budget_rub: [campaign_id],
    )
    async def set_daily_budget(self, campaign_id: int, new_budget_rub: int) -> str:
        self.calls.append({"campaign_id": campaign_id, "new_budget_rub": new_budget_rub})
        return "ok"


class _FakeServiceNoRationale:
    """Service that does NOT expose ``_resolve_rationale_store``.

    Models the backward-compat case: existing services landed before
    M20 and don't know about the rationale store. The decorator must
    keep working without raising AttributeError.
    """

    def __init__(self, pipeline: _StubPipeline, store: PendingPlansStore) -> None:
        self._pipeline = pipeline
        self._plans_store = store
        self.calls: list[dict[str, Any]] = []

    def _resolve_safety(self) -> tuple[_StubPipeline, PendingPlansStore]:
        return self._pipeline, self._plans_store

    @requires_plan(
        action="set_campaign_budget",
        resource_type="campaign",
        preview_builder=lambda self, *a: "x",
        context_builder=_async_ctx_builder,
        resource_ids_from_args=lambda self, *a: [1],
    )
    async def set_daily_budget(self, campaign_id: int, new_budget_rub: int) -> str:
        self.calls.append({"campaign_id": campaign_id})
        return "ok"


@pytest.fixture
def store(tmp_path: Path) -> PendingPlansStore:
    return PendingPlansStore(tmp_path / "pending_plans.jsonl")


@pytest.fixture
def rationale_store(tmp_path: Path) -> RationaleStore:
    return RationaleStore(tmp_path / "rationale.jsonl")


def _build_rationale(decision_id: str = "caller-provided") -> Rationale:
    from datetime import UTC, datetime

    return Rationale(
        decision_id=decision_id,
        action="campaigns.set_daily_budget",
        resource_type="campaign",
        resource_ids=[1],
        summary="lowering budget because CPA crept above target",
        inputs=[
            InputDataPoint(
                name="cpa_rub_7d",
                value=850.0,
                source="metrika",
                observed_at=datetime(2026, 4, 20, 12, 0, tzinfo=UTC),
            ),
        ],
        confidence=Confidence.HIGH,
    )


# --------------------------------------------------------------------------
# Allow path.
# --------------------------------------------------------------------------


class TestRationaleEmissionOnAllow:
    async def test_rationale_persisted_on_allow(
        self,
        store: PendingPlansStore,
        rationale_store: RationaleStore,
    ) -> None:
        pipe = _StubPipeline()  # default: allow
        svc = _FakeServiceWithRationale(pipe, store, rationale_store)
        rationale = _build_rationale()

        await svc.set_daily_budget(1, 200, rationale=rationale)

        # The wrapped method ran and the rationale was recorded.
        assert svc.calls == [{"campaign_id": 1, "new_budget_rub": 200}]
        plan = pipe.review_calls[0][0]
        # decision_id is overwritten to plan_id — caller-provided id was
        # "caller-provided" but the persisted record bears plan.plan_id.
        recorded = rationale_store.get(plan.plan_id)
        assert recorded is not None
        assert recorded.decision_id == plan.plan_id
        assert recorded.summary == rationale.summary
        assert recorded.confidence == Confidence.HIGH

    async def test_caller_decision_id_does_not_leak(
        self,
        store: PendingPlansStore,
        rationale_store: RationaleStore,
    ) -> None:
        # The caller's decision_id ("caller-provided") must not survive
        # — without this guard a buggy caller could persist multiple
        # rationales under the same string id, or worse, overwrite an
        # existing decision's record.
        pipe = _StubPipeline()
        svc = _FakeServiceWithRationale(pipe, store, rationale_store)

        await svc.set_daily_budget(1, 200, rationale=_build_rationale("caller-provided"))

        assert rationale_store.get("caller-provided") is None


# --------------------------------------------------------------------------
# Confirm path.
# --------------------------------------------------------------------------


class TestRationaleEmissionOnConfirm:
    async def test_rationale_persisted_on_confirm(
        self,
        store: PendingPlansStore,
        rationale_store: RationaleStore,
    ) -> None:
        from yadirect_agent.agent.executor import PlanRequired

        pipe = _StubPipeline(
            next_decision=SafetyDecision(status="confirm", reason="needs human ok"),
        )
        svc = _FakeServiceWithRationale(pipe, store, rationale_store)
        rationale = _build_rationale()

        with pytest.raises(PlanRequired) as exc:
            await svc.set_daily_budget(1, 200, rationale=rationale)

        # The plan persisted; rationale persisted under the same id.
        plan_id = exc.value.plan_id
        assert store.get(plan_id) is not None
        recorded = rationale_store.get(plan_id)
        assert recorded is not None
        assert recorded.decision_id == plan_id


# --------------------------------------------------------------------------
# Reject path.
# --------------------------------------------------------------------------


class TestRationaleSkippedOnReject:
    async def test_rationale_not_persisted_on_reject(
        self,
        store: PendingPlansStore,
        rationale_store: RationaleStore,
    ) -> None:
        from yadirect_agent.agent.executor import PlanRejected

        pipe = _StubPipeline(
            next_decision=SafetyDecision(status="reject", reason="cap exceeded"),
        )
        svc = _FakeServiceWithRationale(pipe, store, rationale_store)
        rationale = _build_rationale()

        with pytest.raises(PlanRejected):
            await svc.set_daily_budget(1, 200, rationale=rationale)

        # No plan persisted (reject contract) AND no rationale either —
        # rationale describes the WHY behind a decision; if the policy
        # rejected the plan, there is no "decision to act on" worth
        # explaining at this layer (the rejection itself is captured
        # by the audit sink).
        assert store.all_plans() == []
        # Empty file = no recorded rationales.
        loaded = list(rationale_store._collapse_by_id().values())  # noqa: SLF001
        assert loaded == []


# --------------------------------------------------------------------------
# Backward compat & soft-optional behaviour.
# --------------------------------------------------------------------------


class TestRationaleSoftOptional:
    async def test_no_rationale_kwarg_warns_but_continues(
        self,
        store: PendingPlansStore,
        rationale_store: RationaleStore,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # Service with rationale store, but caller didn't pass
        # rationale=. Soft-optional contract: warn, don't raise.
        pipe = _StubPipeline()
        svc = _FakeServiceWithRationale(pipe, store, rationale_store)

        with caplog.at_level(logging.WARNING):
            result = await svc.set_daily_budget(1, 200)

        assert result == "ok"
        # Empty file = no rationale recorded.
        loaded = list(rationale_store._collapse_by_id().values())  # noqa: SLF001
        assert loaded == []

    async def test_rationale_without_store_warns_but_continues(
        self,
        store: PendingPlansStore,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # Service that doesn't expose _resolve_rationale_store at all.
        # Backward-compat: must keep working as before M20.
        pipe = _StubPipeline()
        svc = _FakeServiceNoRationale(pipe, store)
        rationale = _build_rationale()

        with caplog.at_level(logging.WARNING):
            result = await svc.set_daily_budget(1, 200, rationale=rationale)

        assert result == "ok"
        assert svc.calls == [{"campaign_id": 1}]


# --------------------------------------------------------------------------
# Apply-plan re-entry path.
# --------------------------------------------------------------------------


class TestRationaleNotReEmittedOnApply:
    async def test_applying_plan_id_bypasses_rationale_emission(
        self,
        store: PendingPlansStore,
        rationale_store: RationaleStore,
    ) -> None:
        # The apply-plan re-entry path passes _applying_plan_id= and
        # short-circuits the whole pipeline. Rationale was already
        # recorded when the plan was first proposed; re-emitting on
        # apply would either duplicate (worst) or contradict (worse)
        # the original record.
        pipe = _StubPipeline()
        svc = _FakeServiceWithRationale(pipe, store, rationale_store)

        # Re-entry path: rationale kwarg is ignored.
        await svc.set_daily_budget(
            1,
            200,
            _applying_plan_id="some-plan-id",
            rationale=_build_rationale(),
        )

        # No new rationale persisted by the apply-plan re-entry.
        loaded = list(rationale_store._collapse_by_id().values())  # noqa: SLF001
        assert loaded == []
