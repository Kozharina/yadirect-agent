"""Tests for @requires_plan decorator and apply_plan executor (M2.2 part 3a).

Scope:
- @requires_plan decorator on an async service method:
  * pipeline `allow`   → method runs; `on_applied` called once.
  * pipeline `confirm` → method does NOT run; plan persisted; `PlanRequired` raised.
  * pipeline `reject`  → method does NOT run; nothing persisted; `PlanRejected` raised.
  * `_applying_plan_id=` bypass → method runs without going through the pipeline.
- apply_plan(plan_id):
  * happy path: re-review `allow` → executor runs → `on_applied` + status `applied`.
  * re-review returns `reject` → status `rejected` + `PlanRejected`; executor NOT called.
  * executor raises → status `failed`; `on_applied` NOT called (TOCTOU invariant).
  * plan not found → raise.
  * plan not in `pending` → raise.

The pipeline/store contract is verified with a stub SafetyPipeline
(captures `review` / `on_applied` calls) and a temp PendingPlansStore.
No real API calls happen anywhere.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from yadirect_agent.agent.executor import (
    InvalidPlanStateError,
    PlanRejected,
    PlanRequired,
    apply_plan,
    requires_plan,
)
from yadirect_agent.agent.pipeline import ReviewContext, SafetyDecision
from yadirect_agent.agent.plans import OperationPlan, PendingPlansStore
from yadirect_agent.agent.safety import (
    AccountBudgetSnapshot,
    BudgetChange,
    CampaignBudget,
    CheckResult,
)

# --------------------------------------------------------------------------
# Stubs.
# --------------------------------------------------------------------------


@dataclass
class _StubPipeline:
    """Captures every `review` + `on_applied` call for assertions.

    Returns a preset SafetyDecision from `review` (default: allow).
    """

    next_decision: SafetyDecision = field(
        default_factory=lambda: SafetyDecision(status="allow", reason="ok")
    )
    review_calls: list[tuple[OperationPlan, ReviewContext]] = field(default_factory=list)
    on_applied_calls: list[ReviewContext] = field(default_factory=list)

    def review(self, plan: OperationPlan, context: ReviewContext) -> SafetyDecision:
        self.review_calls.append((plan, context))
        return self.next_decision

    def on_applied(self, context: ReviewContext) -> None:
        self.on_applied_calls.append(context)


def _snap() -> AccountBudgetSnapshot:
    return AccountBudgetSnapshot(
        campaigns=[
            CampaignBudget(id=1, name="c", daily_budget_rub=100, state="ON"),
        ]
    )


def _ctx() -> ReviewContext:
    return ReviewContext(
        budget_snapshot=_snap(),
        budget_changes=[BudgetChange(campaign_id=1, new_daily_budget_rub=150)],
    )


class _FakeService:
    """Minimal async service that exposes one decorated method.

    The decorator resolves (pipeline, store) via `_resolve_safety`;
    that's the contract the real `CampaignService` will implement
    in PR B. Keeping the contract explicit here means the decorator
    has no hidden knowledge about specific service classes.
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
        preview_builder=lambda self, campaign_id, new_budget_rub: (
            f"set budget on campaign {campaign_id} to {new_budget_rub} RUB"
        ),
        context_builder=lambda self, campaign_id, new_budget_rub: _ctx(),
        resource_ids_from_args=lambda self, campaign_id, new_budget_rub: [campaign_id],
    )
    async def set_daily_budget(self, campaign_id: int, new_budget_rub: int) -> str:
        self.calls.append({"campaign_id": campaign_id, "new_budget_rub": new_budget_rub})
        return "ok"


@pytest.fixture
def store(tmp_path: Path) -> PendingPlansStore:
    return PendingPlansStore(tmp_path / "pending_plans.jsonl")


# --------------------------------------------------------------------------
# @requires_plan decorator.
# --------------------------------------------------------------------------


class TestRequiresPlanAllow:
    async def test_allow_runs_method_and_calls_on_applied(self, store: PendingPlansStore) -> None:
        pipe = _StubPipeline()  # default: allow
        svc = _FakeService(pipe, store)

        result = await svc.set_daily_budget(1, 200)

        assert result == "ok"
        assert svc.calls == [{"campaign_id": 1, "new_budget_rub": 200}]
        assert len(pipe.review_calls) == 1
        assert len(pipe.on_applied_calls) == 1
        # No plan persisted on allow — review alone is enough.
        assert store.all_plans() == []

    async def test_allow_passes_context_to_on_applied(self, store: PendingPlansStore) -> None:
        # on_applied must receive the SAME context that review evaluated,
        # not a rebuilt one. This guarantees the session TOCTOU register
        # records against the originally-approved ceiling.
        pipe = _StubPipeline()
        svc = _FakeService(pipe, store)
        await svc.set_daily_budget(1, 200)
        review_ctx = pipe.review_calls[0][1]
        applied_ctx = pipe.on_applied_calls[0]
        assert applied_ctx is review_ctx


class TestRequiresPlanConfirm:
    async def test_confirm_raises_plan_required_and_persists(
        self, store: PendingPlansStore
    ) -> None:
        pipe = _StubPipeline(
            next_decision=SafetyDecision(status="confirm", reason="change exceeds ceiling")
        )
        svc = _FakeService(pipe, store)

        with pytest.raises(PlanRequired) as exc:
            await svc.set_daily_budget(1, 200)

        # Exception carries enough metadata for the CLI to tell the
        # operator what to do next.
        assert exc.value.plan_id
        assert "campaign 1" in exc.value.preview
        # Method was NOT invoked; pipeline.on_applied NOT called.
        assert svc.calls == []
        assert pipe.on_applied_calls == []
        # Plan persisted with pending status + review_context populated.
        stored = store.get(exc.value.plan_id)
        assert stored is not None
        assert stored.status == "pending"
        assert stored.action == "set_campaign_budget"
        assert stored.resource_ids == [1]
        assert stored.args == {"campaign_id": 1, "new_budget_rub": 200}
        assert stored.review_context is not None


class TestRequiresPlanReject:
    async def test_reject_raises_and_does_not_persist(self, store: PendingPlansStore) -> None:
        pipe = _StubPipeline(
            next_decision=SafetyDecision(
                status="reject",
                reason="exceeds account cap",
                blocking_checks=[CheckResult(status="blocked", reason="budget_cap: x")],
            )
        )
        svc = _FakeService(pipe, store)

        with pytest.raises(PlanRejected) as exc:
            await svc.set_daily_budget(1, 200)

        assert "cap" in exc.value.reason
        # blocking is the raw CheckResult list — surfaces reason + details
        # to the CLI / audit sink without flattening.
        assert len(exc.value.blocking) == 1
        assert exc.value.blocking[0].status == "blocked"
        assert "budget_cap" in (exc.value.blocking[0].reason or "")
        assert svc.calls == []
        assert pipe.on_applied_calls == []
        # Reject does NOT write to the jsonl store — that's audit-sink
        # territory (M2.3), not plan-store territory.
        assert store.all_plans() == []


class TestRequiresPlanBypass:
    async def test_applying_plan_id_skips_pipeline(self, store: PendingPlansStore) -> None:
        # apply-plan calls the decorated method with _applying_plan_id=
        # to avoid a double review loop (apply_plan has already re-reviewed
        # the plan before routing here). The decorator must run the
        # wrapped method directly and NOT touch the pipeline or store.
        pipe = _StubPipeline(next_decision=SafetyDecision(status="reject", reason="would reject"))
        svc = _FakeService(pipe, store)

        result = await svc.set_daily_budget(1, 200, _applying_plan_id="xyz")  # type: ignore[call-arg]

        assert result == "ok"
        assert svc.calls == [{"campaign_id": 1, "new_budget_rub": 200}]
        # No review, no on_applied — apply_plan handles those itself.
        assert pipe.review_calls == []
        assert pipe.on_applied_calls == []


# --------------------------------------------------------------------------
# apply_plan executor.
# --------------------------------------------------------------------------


async def _route_set_budget(
    svc: _FakeService, action: str, args: dict[str, Any], *, _applying_plan_id: str
) -> Any:
    """Tiny routing shim — maps action name to the service method.

    The real CLI wiring (PR B) has a richer router; the executor
    contract is the same: (action, args, _applying_plan_id) → result.
    """
    if action == "set_campaign_budget":
        return await svc.set_daily_budget(
            args["campaign_id"],
            args["new_budget_rub"],
            _applying_plan_id=_applying_plan_id,  # type: ignore[call-arg]
        )
    msg = f"unknown action: {action}"
    raise ValueError(msg)


async def _seed_pending_plan(store: PendingPlansStore, pipe: _StubPipeline) -> str:
    """Use the decorator's own confirm path to seed a realistic plan."""
    pipe.next_decision = SafetyDecision(status="confirm", reason="needs confirm")
    svc = _FakeService(pipe, store)
    try:
        await svc.set_daily_budget(1, 200)
    except PlanRequired as exc:
        pipe.review_calls.clear()
        pipe.on_applied_calls.clear()
        return exc.plan_id
    msg = "expected PlanRequired"
    raise AssertionError(msg)


class TestApplyPlanHappy:
    async def test_happy_path_marks_applied_and_calls_on_applied(
        self, store: PendingPlansStore
    ) -> None:
        pipe = _StubPipeline()
        plan_id = await _seed_pending_plan(store, pipe)
        svc = _FakeService(pipe, store)

        # Re-review returns allow now (snapshot no longer triggers confirm).
        pipe.next_decision = SafetyDecision(status="allow", reason="ok")

        result = await apply_plan(
            plan_id,
            store=store,
            pipeline=pipe,
            service_router=lambda action, args, _applying_plan_id: _route_set_budget(
                svc, action, args, _applying_plan_id=_applying_plan_id
            ),
        )

        assert result == "ok"
        assert svc.calls == [{"campaign_id": 1, "new_budget_rub": 200}]
        # on_applied called exactly once — AFTER the executor succeeds.
        assert len(pipe.on_applied_calls) == 1
        # Store status: pending → applied.
        final = store.get(plan_id)
        assert final is not None
        assert final.status == "applied"


class TestApplyPlanReReviewRejects:
    async def test_re_review_reject_marks_rejected_and_skips_executor(
        self, store: PendingPlansStore
    ) -> None:
        pipe = _StubPipeline()
        plan_id = await _seed_pending_plan(store, pipe)
        svc = _FakeService(pipe, store)

        # Between plan creation and apply, something about the snapshot
        # pushed the plan into a reject path (e.g. account cap lowered).
        pipe.next_decision = SafetyDecision(
            status="reject",
            reason="now exceeds cap",
            blocking_checks=[CheckResult(status="blocked", reason="budget_cap: x")],
        )

        with pytest.raises(PlanRejected):
            await apply_plan(
                plan_id,
                store=store,
                pipeline=pipe,
                service_router=lambda action, args, _applying_plan_id: _route_set_budget(
                    svc, action, args, _applying_plan_id=_applying_plan_id
                ),
            )

        # Executor was NOT called, on_applied was NOT called.
        assert svc.calls == []
        assert pipe.on_applied_calls == []
        final = store.get(plan_id)
        assert final is not None
        assert final.status == "rejected"


class TestApplyPlanExecutorFailure:
    async def test_executor_raises_marks_failed_and_skips_on_applied(
        self, store: PendingPlansStore
    ) -> None:
        """Auditor-blocker acceptance test from docs/BACKLOG.md:
        on failure path, session TOCTOU register MUST NOT be updated.
        """
        pipe = _StubPipeline()
        plan_id = await _seed_pending_plan(store, pipe)
        pipe.next_decision = SafetyDecision(status="allow", reason="ok")

        async def failing_router(
            action: str, args: dict[str, Any], *, _applying_plan_id: str
        ) -> Any:
            raise RuntimeError("API 500")

        with pytest.raises(RuntimeError, match="API 500"):
            await apply_plan(
                plan_id,
                store=store,
                pipeline=pipe,
                service_router=failing_router,
            )

        # The critical invariant: on_applied was NOT called.
        assert pipe.on_applied_calls == []
        # Status: pending → failed.
        final = store.get(plan_id)
        assert final is not None
        assert final.status == "failed"


class TestApplyPlanPreconditions:
    async def test_unknown_plan_id_raises_keyerror(self, store: PendingPlansStore) -> None:
        pipe = _StubPipeline()
        with pytest.raises(KeyError):
            await apply_plan(
                "does-not-exist",
                store=store,
                pipeline=pipe,
                service_router=lambda *a, **kw: None,  # type: ignore[arg-type]
            )

    async def test_applied_plan_cannot_be_re_applied(self, store: PendingPlansStore) -> None:
        pipe = _StubPipeline()
        plan_id = await _seed_pending_plan(store, pipe)
        store.update_status(plan_id, "applied")

        with pytest.raises(InvalidPlanStateError):
            await apply_plan(
                plan_id,
                store=store,
                pipeline=pipe,
                service_router=lambda *a, **kw: None,  # type: ignore[arg-type]
            )

    async def test_rejected_plan_cannot_be_applied(self, store: PendingPlansStore) -> None:
        pipe = _StubPipeline()
        plan_id = await _seed_pending_plan(store, pipe)
        store.update_status(plan_id, "rejected")

        with pytest.raises(InvalidPlanStateError):
            await apply_plan(
                plan_id,
                store=store,
                pipeline=pipe,
                service_router=lambda *a, **kw: None,  # type: ignore[arg-type]
            )

    async def test_failed_plan_cannot_be_applied(self, store: PendingPlansStore) -> None:
        # Auditor M-1: ``failed`` is a terminal status added in this PR.
        # Re-applying a failed plan must raise — operators triage and
        # propose a fresh plan; no silent retry through the same record.
        pipe = _StubPipeline()
        plan_id = await _seed_pending_plan(store, pipe)
        store.update_status(plan_id, "failed")

        with pytest.raises(InvalidPlanStateError):
            await apply_plan(
                plan_id,
                store=store,
                pipeline=pipe,
                service_router=lambda *a, **kw: None,  # type: ignore[arg-type]
            )

    async def test_plan_with_no_review_context_cannot_be_applied(
        self, store: PendingPlansStore
    ) -> None:
        # Auditor H-2: a plan inserted directly into the store without
        # going through @requires_plan (e.g. legacy migration data) has
        # review_context=None. apply_plan must reject explicitly with
        # InvalidPlanStateError — not let it fall through to a confusing
        # AttributeError on deserialize_review_context(None).
        from datetime import UTC, datetime

        from yadirect_agent.agent.plans import OperationPlan

        legacy_plan = OperationPlan(
            plan_id="legacy_no_ctx",
            created_at=datetime.now(UTC),
            action="set_campaign_budget",
            resource_type="campaign",
            resource_ids=[1],
            args={"campaign_id": 1, "new_budget_rub": 200},
            preview="legacy plan without review_context",
            reason="seeded by hand",
            # review_context deliberately omitted (defaults to None).
        )
        store.append(legacy_plan)

        pipe = _StubPipeline()
        with pytest.raises(InvalidPlanStateError, match="review_context"):
            await apply_plan(
                legacy_plan.plan_id,
                store=store,
                pipeline=pipe,
                service_router=lambda *a, **kw: None,  # type: ignore[arg-type]
            )


# --------------------------------------------------------------------------
# on_applied invariants (auditor C-1).
# --------------------------------------------------------------------------


class TestApplyPlanOnAppliedRobustness:
    async def test_on_applied_failure_does_not_leave_plan_pending(
        self, store: PendingPlansStore
    ) -> None:
        """Auditor C-1: if pipeline.on_applied raises after a successful
        API write, the plan MUST already be marked ``applied`` — otherwise
        a subsequent apply-plan call would re-execute the API and double-spend.
        """
        plan_id = await _seed_pending_plan(store, _StubPipeline())

        # Custom pipeline whose on_applied raises.
        class _FailingOnApplied(_StubPipeline):
            def on_applied(self, context: ReviewContext) -> None:  # type: ignore[override]
                self.on_applied_calls.append(context)
                msg = "TOCTOU register update failed"
                raise RuntimeError(msg)

        pipe = _FailingOnApplied()
        pipe.next_decision = SafetyDecision(status="allow", reason="ok")
        svc = _FakeService(pipe, store)

        # Executor itself succeeds — the failure is in the post-success
        # session-state hook, not the API call.
        result = await apply_plan(
            plan_id,
            store=store,
            pipeline=pipe,
            service_router=lambda action, args, _applying_plan_id: _route_set_budget(
                svc, action, args, _applying_plan_id=_applying_plan_id
            ),
        )

        # The API call returned successfully; apply_plan must surface that.
        assert result == "ok"
        # And the plan MUST be applied — not pending — so a retry can't
        # double-spend.
        final = store.get(plan_id)
        assert final is not None
        assert final.status == "applied"


# --------------------------------------------------------------------------
# _bound_args_dict — direct unit tests (auditor L-2).
# --------------------------------------------------------------------------


class TestBoundArgsDict:
    """Direct tests on the private signature-binding helper.

    Indirect coverage via the decorator misses the bind-failure paths
    auditor H-1 / M-2 surfaced — exercise them here.
    """

    @staticmethod
    def _make(fn_args: tuple[Any, ...], fn_kwargs: dict[str, Any], fn: Any) -> dict[str, Any]:
        from yadirect_agent.agent.executor import _bound_args_dict

        return _bound_args_dict(fn, fn_args, fn_kwargs)

    def test_basic_positional_binds_to_param_names(self) -> None:
        # ``pass`` over ``...`` to keep CodeQL's py/ineffectual-statement
        # rule quiet on the synthetic function bodies; the body never
        # runs (we only inspect the signature).
        async def f(self: Any, a: int, b: int) -> None:
            pass

        assert self._make((1, 2), {}, f) == {"a": 1, "b": 2}

    def test_keyword_binds_to_param_names(self) -> None:
        async def f(self: Any, a: int, b: int) -> None:
            pass

        assert self._make((), {"a": 1, "b": 2}, f) == {"a": 1, "b": 2}

    def test_omitted_default_is_filled_in(self) -> None:
        # Auditor M-2: defaulted params must be captured at call time so
        # a deployment with a changed default replays the original intent.
        async def f(self: Any, a: int, b: int = 99) -> None:
            pass

        assert self._make((1,), {}, f) == {"a": 1, "b": 99}

    def test_overflow_positional_args_raise(self) -> None:
        # Auditor H-1: silent drop is a faithful-replay hazard. bind()
        # must surface it as TypeError.
        async def f(self: Any, a: int) -> None:
            pass

        with pytest.raises(TypeError):
            self._make((1, 2, 3), {}, f)

    def test_unknown_kwarg_raises(self) -> None:
        async def f(self: Any, a: int) -> None:
            pass

        with pytest.raises(TypeError):
            self._make((), {"a": 1, "mystery": 2}, f)

    def test_var_positional_preserved_as_tuple(self) -> None:
        # Auditor H-1: *args must round-trip as a tuple, not be
        # collapsed to its first element.
        async def f(self: Any, a: int, *ids: int) -> None:
            pass

        result = self._make((1, 10, 20, 30), {}, f)
        assert result == {"a": 1, "ids": (10, 20, 30)}

    def test_applying_plan_id_is_filtered(self) -> None:
        # The bypass kwarg must never reach the wrapped function's
        # signature, so it must not appear in plan.args either.
        async def f(self: Any, a: int) -> None:
            pass

        assert self._make((), {"a": 1, "_applying_plan_id": "xyz"}, f) == {"a": 1}
