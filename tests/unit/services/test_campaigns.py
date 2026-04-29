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
from yadirect_agent.models.rationale import Rationale
from yadirect_agent.services.campaigns import CampaignService, CampaignSummary


def _test_rationale(
    *, action: str = "set_campaign_budget", resource_ids: list[int] | None = None
) -> Rationale:
    """Minimal valid Rationale for tests focused on decision mechanics.

    M20 slice 2 made ``rationale=`` hard-required on every
    @requires_plan call site. Tests that exercise the service's
    decisions (state filtering, validation, audit emission) — not
    rationale shape — pass this stub. Tests that DO exercise
    rationale content live in ``test_executor_rationale.py``.
    """
    return Rationale(
        decision_id="test-placeholder",
        action=action,
        resource_type="campaign",
        resource_ids=resource_ids if resource_ids is not None else [1],
        summary="test rationale — exercising service mechanics, not rationale content.",
    )


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

    def test_does_not_carry_negative_keywords_to_agent_surface(self) -> None:
        """Defence-in-depth: ``CampaignSummary`` is the agent-facing
        view (returned by ``list_campaigns`` tool and CLI ``--json``).
        Operator-configured negatives are commercial intent — brand
        misspells, competitor names — and have no business reaching
        the LLM agent's response. They flow only through the
        safety-internal path (``_build_account_budget_snapshot`` →
        ``CampaignBudget.negative_keywords``).

        Pin: a Campaign rich with negatives produces a summary that
        does NOT expose them. A future refactor that adds the field
        for convenience would silently leak operator strategy to
        the LLM."""
        c = Campaign.model_validate(
            {
                "Id": 4,
                "Name": "c4",
                "NegativeKeywords": {"Items": ["бесплатно", "отзывы"]},
            }
        )

        summary = CampaignSummary.from_model(c)

        # No negative_keywords attribute on the summary at all.
        assert not hasattr(summary, "negative_keywords")


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
    # Bypass the @requires_plan decorator; we're only testing the
    # client-delegation contract (the decorator's behaviour is
    # exercised separately).
    await CampaignService(settings).pause([1, 2, 3], _applying_plan_id="test-bypass")

    assert fake_direct.suspend_calls == [[1, 2, 3]]
    assert fake_direct.resume_calls == []


@pytest.mark.asyncio
async def test_resume_delegates_to_client_resume(
    settings: Settings, fake_direct: _FakeDirectService
) -> None:
    await CampaignService(settings).resume([7], _applying_plan_id="test-bypass")

    assert fake_direct.resume_calls == [[7]]
    assert fake_direct.suspend_calls == []


# --------------------------------------------------------------------------
# set_daily_budget: enforces Direct's minimum.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_daily_budget_rejects_below_minimum(
    settings: Settings, fake_direct: _FakeDirectService
) -> None:
    # Bypass the @requires_plan decorator with _applying_plan_id so we
    # exercise the inner validation path (the decorator now wraps this
    # method; without bypass we'd hit _resolve_safety before validation).
    with pytest.raises(ValueError, match=">= 300 RUB"):
        await CampaignService(settings).set_daily_budget(
            campaign_id=1, budget_rub=299, _applying_plan_id="test-bypass"
        )

    # We rejected early — the client must not have been called.
    assert fake_direct.budget_calls == []


@pytest.mark.asyncio
async def test_set_daily_budget_accepts_minimum(
    settings: Settings, fake_direct: _FakeDirectService
) -> None:
    await CampaignService(settings).set_daily_budget(
        campaign_id=42, budget_rub=300, _applying_plan_id="test-bypass"
    )

    assert fake_direct.budget_calls == [(42, 300, "STANDARD")]


@pytest.mark.asyncio
async def test_set_daily_budget_passes_through_amount_in_rubles(
    settings: Settings, fake_direct: _FakeDirectService
) -> None:
    # The service speaks rubles; the client is responsible for converting to
    # micro-currency. We verify that contract here.
    await CampaignService(settings).set_daily_budget(
        campaign_id=42, budget_rub=1500, _applying_plan_id="test-bypass"
    )

    assert fake_direct.budget_calls == [(42, 1500, "STANDARD")]


# --------------------------------------------------------------------------
# @requires_plan wiring on set_daily_budget (M2.2 part 3b1).
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_daily_budget_without_safety_raises_runtime_error(
    settings: Settings, fake_direct: _FakeDirectService
) -> None:
    """Passing no pipeline/store but calling set_daily_budget without the
    bypass kwarg must fail loudly. Silent fallback would be a security hole
    — every mutating method must be gated unless explicitly opted out via
    the apply-plan re-entry path.
    """
    from yadirect_agent.agent.executor import requires_plan  # noqa: F401

    svc = CampaignService(settings)  # no pipeline / no store
    with pytest.raises(RuntimeError, match="SafetyPipeline"):
        await svc.set_daily_budget(campaign_id=42, budget_rub=500, rationale=_test_rationale())


@pytest.mark.asyncio
async def test_set_daily_budget_with_safety_persists_plan_on_confirm(
    settings: Settings, fake_direct: _FakeDirectService, tmp_path: Any
) -> None:
    """End-to-end: a budget change above the auto-approve threshold (which
    today is *every* budget change since auto_approve_budget_change isn't
    a knob) flows through @requires_plan → pipeline.review → confirm,
    producing a persisted OperationPlan in the JSONL store and raising
    PlanRequired without touching DirectService.
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

    # Seed the fake API with one campaign so list_all() in context_builder
    # has something to convert into AccountBudgetSnapshot.
    fake_direct._campaigns = [
        Campaign(
            id=42,
            name="alpha",
            state=CampaignState.ON,
            status=CampaignStatus.ACCEPTED,
            type="TEXT_CAMPAIGN",
            daily_budget=DailyBudget(amount=500_000_000, mode="STANDARD"),
        )
    ]

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

    svc = CampaignService(settings, pipeline=pipeline, store=store)

    # Raise budget from 500 → 800 RUB. Within the +20% per-call ceiling
    # and well under the 100,000 account cap, so the kill-switches pass.
    # But there is no auto_approve_budget_change knob, so the approval-tier
    # check returns confirm.
    with pytest.raises(PlanRequired) as exc:
        await svc.set_daily_budget(
            campaign_id=42, budget_rub=800, rationale=_test_rationale(resource_ids=[42])
        )

    # Plan was persisted, with the canonical action name and raw args.
    assert exc.value.plan_id
    plan = store.get(exc.value.plan_id)
    assert plan is not None
    assert plan.status == "pending"
    assert plan.action == "set_campaign_budget"
    assert plan.resource_ids == [42]
    assert plan.args == {"campaign_id": 42, "budget_rub": 800}
    # review_context populated so apply-plan can re-review later.
    assert plan.review_context is not None

    # The DirectService client was NOT called.
    assert fake_direct.budget_calls == []


# --------------------------------------------------------------------------
# Audit emission on the service path (M2.3b).
#
# CampaignService.set_daily_budget emits ``set_campaign_budget.requested``
# and ``set_campaign_budget.ok`` (or .failed) via the audit_sink the
# constructor receives. Actor is determined by call shape:
#   - apply-plan re-entry (``_applying_plan_id`` present) → "human"
#   - direct call from the agent's allow-path → "agent"
# --------------------------------------------------------------------------


class _CapturingSink:
    """In-memory AuditSink stub for tests."""

    def __init__(self) -> None:
        from yadirect_agent.audit import AuditEvent

        self.events: list[AuditEvent] = []

    async def emit(self, event: Any) -> None:
        self.events.append(event)


@pytest.mark.asyncio
async def test_set_daily_budget_emits_requested_and_ok_on_apply_plan_path(
    settings: Settings, fake_direct: _FakeDirectService
) -> None:
    """When the operator runs apply-plan (bypass kwarg present), the
    service-level audit MUST fire as ``actor="human"`` so the JSONL
    line ties back to the operator action, not a phantom agent call.
    """
    sink = _CapturingSink()
    svc = CampaignService(settings, audit_sink=sink)

    await svc.set_daily_budget(
        campaign_id=42,
        budget_rub=500,
        _applying_plan_id="plan-xyz",
    )

    assert len(sink.events) == 2
    requested, ok = sink.events
    assert requested.action == "set_campaign_budget.requested"
    assert requested.actor == "human"
    assert requested.resource == "campaign:42"
    assert requested.args == {"campaign_id": 42, "budget_rub": 500}
    assert requested.result is None

    assert ok.action == "set_campaign_budget.ok"
    assert ok.actor == "human"
    assert ok.result is not None
    assert ok.result["status"] == "applied"
    assert ok.result["campaign_id"] == 42


@pytest.mark.asyncio
async def test_set_daily_budget_emits_failed_when_api_raises(
    settings: Settings, fake_direct: _FakeDirectService, monkeypatch: pytest.MonkeyPatch
) -> None:
    sink = _CapturingSink()
    svc = CampaignService(settings, audit_sink=sink)

    # Make DirectService raise when the budget update is sent.
    async def boom(self: _FakeDirectService, campaign_id: int, budget_rub: int) -> None:
        raise RuntimeError("API 500")

    monkeypatch.setattr(_FakeDirectService, "update_campaign_budget", boom)

    with pytest.raises(RuntimeError, match="API 500"):
        await svc.set_daily_budget(campaign_id=42, budget_rub=500, _applying_plan_id="plan-xyz")

    assert len(sink.events) == 2
    requested, failed = sink.events
    assert requested.action == "set_campaign_budget.requested"
    assert failed.action == "set_campaign_budget.failed"
    assert failed.result is not None
    assert failed.result["error_type"] == "RuntimeError"
    assert failed.result["error_message"] == "API 500"


@pytest.mark.asyncio
async def test_set_daily_budget_without_audit_sink_runs_unchanged(
    settings: Settings, fake_direct: _FakeDirectService
) -> None:
    """Backwards-compat: callers that don't pass an audit_sink (test
    fixtures, future read-only code paths) keep working — the audit
    layer is opt-in by sink presence so existing tests are not
    forced to thread one through.
    """
    svc = CampaignService(settings)  # no audit_sink

    await svc.set_daily_budget(campaign_id=42, budget_rub=500, _applying_plan_id="bypass")

    assert fake_direct.budget_calls == [(42, 500, "STANDARD")]


@pytest.mark.asyncio
async def test_set_daily_budget_emits_actor_agent_on_full_decorator_path(
    settings: Settings, fake_direct: _FakeDirectService, tmp_path: Any
) -> None:
    """Auditor HIGH: the actor inference must report ``agent`` for
    calls that go through the @requires_plan decorator's allow path,
    not just for the bypass path. We exercise this by configuring the
    policy so the proposed mutation passes the kill-switches AND the
    approval tier (``auto_approve_pause`` is the only currently-
    auto-approvable action that lands in CampaignService, but
    ``set_daily_budget`` requires confirm by default — so we use the
    confirm path's persisted plan as the proxy: when re-applied via
    apply_plan, the inner emit is human; when the agent's allow path
    fires it would emit agent).

    Since today's policy has no ``auto_approve_budget_change`` knob,
    the agent's set_daily_budget call ALWAYS lands as confirm — the
    inner emit doesn't fire on the agent path. So the cleanest
    actor=agent assertion is via a synthetic test that bypasses the
    decorator (calls the underlying _do_set_daily_budget directly
    after wrapping in audit_action with explicit actor) — but that's
    asserting the audit layer, not the actor inference.

    The actually-meaningful test for actor=agent is: the production
    call path produces NO ``set_campaign_budget`` audit event at all
    on the agent flow today, because the decorator returns confirm
    before the wrapped method runs. This test pins that contract
    instead — the only way ``actor=agent`` events fire is if a
    future ``auto_approve_budget_change`` knob lands. Until then, the
    agent path emits zero service-level audit events for budget
    changes, only the @requires_plan decision (out-of-scope for this
    PR) and the apply_plan envelope (actor=human).
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

    fake_direct._campaigns = [
        Campaign(
            id=42,
            name="alpha",
            state=CampaignState.ON,
            status=CampaignStatus.ACCEPTED,
            type="TEXT_CAMPAIGN",
            daily_budget=DailyBudget(amount=500_000_000, mode="STANDARD"),
        )
    ]
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

    svc = CampaignService(settings, pipeline=pipeline, store=store, audit_sink=sink)

    # Agent path: the decorator returns confirm BEFORE the wrapped
    # body runs, so no ``set_campaign_budget.*`` event fires.
    with pytest.raises(PlanRequired):
        await svc.set_daily_budget(
            campaign_id=42, budget_rub=800, rationale=_test_rationale(resource_ids=[42])
        )

    # No service-level audit events on the agent confirm path.
    assert [e.action for e in sink.events if "set_campaign_budget" in e.action] == []
    # The DirectService client was NOT called.
    assert fake_direct.budget_calls == []


@pytest.mark.asyncio
async def test_set_daily_budget_through_apply_plan_emits_actor_human(
    settings: Settings, fake_direct: _FakeDirectService, tmp_path: Any
) -> None:
    """Auditor HIGH: drive the FULL apply_plan → router → decorated
    method chain and assert the inner ``set_campaign_budget.*`` audit
    pair carries ``actor="human"`` — i.e. the frame walk located the
    decorator's wrapper frame, not a test-function frame that happens
    to declare ``_applying_plan_id``.
    """
    from yadirect_agent.agent.executor import apply_plan
    from yadirect_agent.agent.pipeline import (
        ReviewContext,
        SafetyPipeline,
        serialize_review_context,
    )
    from yadirect_agent.agent.plans import OperationPlan, PendingPlansStore
    from yadirect_agent.agent.safety import (
        AccountBudgetSnapshot,
        BudgetCapPolicy,
        BudgetChange,
        CampaignBudget,
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

    # Seed a pending plan with a real ReviewContext.
    ctx = ReviewContext(
        budget_snapshot=AccountBudgetSnapshot(
            campaigns=[
                CampaignBudget(id=42, name="alpha", daily_budget_rub=500.0, state="ON"),
            ]
        ),
        budget_changes=[BudgetChange(campaign_id=42, new_daily_budget_rub=800)],
    )
    from datetime import UTC, datetime

    plan = OperationPlan(
        plan_id="plan-go",
        created_at=datetime(2026, 4, 27, 12, 0, tzinfo=UTC),
        action="set_campaign_budget",
        resource_type="campaign",
        resource_ids=[42],
        args={"campaign_id": 42, "budget_rub": 800},
        preview="raise budget",
        reason="confirm",
        review_context=serialize_review_context(ctx),
    )
    store.append(plan)

    svc = CampaignService(settings, pipeline=pipeline, store=store, audit_sink=sink)

    async def router(action: str, args: Any, *, _applying_plan_id: str) -> Any:
        if action == "set_campaign_budget":
            return await svc.set_daily_budget(**args, _applying_plan_id=_applying_plan_id)
        raise ValueError(action)

    await apply_plan(
        "plan-go",
        store=store,
        pipeline=pipeline,
        service_router=router,
        audit_sink=sink,
    )

    # Inner set_campaign_budget pair fires from the decorated path —
    # frame walk MUST find the @requires_plan wrapper and label both
    # events ``actor="human"``.
    inner = [e for e in sink.events if e.action.startswith("set_campaign_budget.")]
    assert len(inner) == 2
    assert all(e.actor == "human" for e in inner)
    assert inner[0].action == "set_campaign_budget.requested"
    assert inner[1].action == "set_campaign_budget.ok"


# --------------------------------------------------------------------------
# pause / resume gated through @requires_plan (M2 follow-up).
# --------------------------------------------------------------------------


def _build_safety_for_test(tmp_path: Any) -> tuple[Any, Any, _CapturingSink]:
    """Helper: minimal safety stack for end-to-end gating tests."""
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
        # auto_approve_pause defaults to True; auto_approve_resume defaults to False.
    )
    return (
        SafetyPipeline(policy),
        PendingPlansStore(tmp_path / "pending_plans.jsonl"),
        _CapturingSink(),
    )


@pytest.mark.asyncio
async def test_pause_through_decorator_passes_allow_path(
    settings: Settings, fake_direct: _FakeDirectService, tmp_path: Any
) -> None:
    """auto_approve_pause=True (default) → pipeline allows → pause
    completes in one shot AND emits the audit pair. The HIGH gap
    "pause/resume bypass the safety pipeline" closed: the wrapper
    runs, KS#1 is consulted (trivially passes since pause lowers
    spend), and the audit JSONL records actor=agent.
    """
    fake_direct._campaigns = [
        Campaign(
            id=1,
            name="alpha",
            state=CampaignState.ON,
            status=CampaignStatus.ACCEPTED,
            type="TEXT_CAMPAIGN",
            daily_budget=DailyBudget(amount=500_000_000, mode="STANDARD"),
        )
    ]
    pipeline, store, sink = _build_safety_for_test(tmp_path)
    svc = CampaignService(settings, pipeline=pipeline, store=store, audit_sink=sink)

    # Allow path: no exception.
    await svc.pause([1], rationale=_test_rationale(action="pause_campaigns"))

    assert fake_direct.suspend_calls == [[1]]
    actions = [e.action for e in sink.events]
    assert "pause_campaigns.requested" in actions
    assert "pause_campaigns.ok" in actions


@pytest.mark.asyncio
async def test_resume_through_decorator_persists_confirm_plan(
    settings: Settings, fake_direct: _FakeDirectService, tmp_path: Any
) -> None:
    """auto_approve_resume=False (default) → pipeline returns confirm
    → resume raises PlanRequired without touching DirectService.
    Resume is the primary KS#3 trigger per safety-spec, so the
    gating is mandatory.
    """
    from yadirect_agent.agent.executor import PlanRequired

    fake_direct._campaigns = [
        Campaign(
            id=1,
            name="alpha",
            state=CampaignState.SUSPENDED,
            status=CampaignStatus.ACCEPTED,
            type="TEXT_CAMPAIGN",
            daily_budget=DailyBudget(amount=500_000_000, mode="STANDARD"),
        )
    ]
    pipeline, store, sink = _build_safety_for_test(tmp_path)
    svc = CampaignService(settings, pipeline=pipeline, store=store, audit_sink=sink)

    with pytest.raises(PlanRequired) as exc:
        await svc.resume([1], rationale=_test_rationale(action="resume_campaigns"))

    # Plan persisted; DirectService NOT called.
    plan = store.get(exc.value.plan_id)
    assert plan is not None
    assert plan.status == "pending"
    assert plan.action == "resume_campaigns"
    assert plan.resource_ids == [1]
    assert plan.args == {"campaign_ids": [1]}
    assert fake_direct.resume_calls == []


# --------------------------------------------------------------------------
# Partial-success guard (auditor M2 follow-up MEDIUM).
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pause_raises_partial_action_error_on_per_item_errors(
    settings: Settings,
    fake_direct: _FakeDirectService,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """Direct returns HTTP 200 with per-item Errors on partial bulk
    success. Without the guard the plan would silently transition to
    ``applied`` while one of three campaigns was still running.
    """
    from yadirect_agent.services.campaigns import PartialActionError

    fake_direct._campaigns = [
        Campaign(
            id=1,
            name="alpha",
            state=CampaignState.ON,
            status=CampaignStatus.ACCEPTED,
            type="TEXT_CAMPAIGN",
            daily_budget=DailyBudget(amount=500_000_000, mode="STANDARD"),
        )
    ]
    pipeline, store, sink = _build_safety_for_test(tmp_path)

    async def fake_suspend(self: _FakeDirectService, ids: list[int]) -> dict[str, Any]:
        # Two ids succeed, one fails — typical partial-success.
        return {
            "SuspendResults": [
                {"Id": 1, "Suspended": 1},
                {"Id": 2, "Errors": [{"Code": 8000, "Message": "Permission denied"}]},
                {"Id": 3, "Suspended": 1},
            ]
        }

    monkeypatch.setattr(_FakeDirectService, "suspend_campaigns", fake_suspend)
    svc = CampaignService(settings, pipeline=pipeline, store=store, audit_sink=sink)

    with pytest.raises(PartialActionError):
        await svc.pause([1, 2, 3], rationale=_test_rationale(action="pause_campaigns"))


@pytest.mark.asyncio
async def test_resume_raises_partial_action_error_on_per_item_errors(
    settings: Settings,
    fake_direct: _FakeDirectService,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    from yadirect_agent.services.campaigns import PartialActionError

    fake_direct._campaigns = [
        Campaign(
            id=1,
            name="alpha",
            state=CampaignState.SUSPENDED,
            status=CampaignStatus.ACCEPTED,
            type="TEXT_CAMPAIGN",
            daily_budget=DailyBudget(amount=500_000_000, mode="STANDARD"),
        )
    ]
    # Use auto_approve_resume=True to take the allow path so we hit
    # the inner _do_resume directly.
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
        auto_approve_resume=True,  # take allow path for this test
    )
    pipeline = SafetyPipeline(policy)
    store = PendingPlansStore(tmp_path / "pending_plans.jsonl")
    sink = _CapturingSink()

    async def fake_resume(self: _FakeDirectService, ids: list[int]) -> dict[str, Any]:
        return {
            "ResumeResults": [
                {"Id": 1, "Errors": [{"Code": 8000, "Message": "x"}]},
            ]
        }

    monkeypatch.setattr(_FakeDirectService, "resume_campaigns", fake_resume)
    svc = CampaignService(settings, pipeline=pipeline, store=store, audit_sink=sink)

    with pytest.raises(PartialActionError):
        await svc.resume([1], rationale=_test_rationale(action="resume_campaigns"))


# --------------------------------------------------------------------------
# KS#3 (negative-keyword floor) — end-to-end firing.
#
# Pre-PR ``_build_resume_context`` populated ``CampaignBudget.negative_keywords``
# with an empty frozenset because the Direct API NegativeKeywords envelope
# wasn't being read. With the model + client + summary changes wired,
# KS#3 sees real per-campaign negatives and correctly fires only when
# the campaign is actually missing a required phrase.
# --------------------------------------------------------------------------


def _build_safety_with_required_negatives(
    tmp_path: Any, required: list[str]
) -> tuple[Any, Any, _CapturingSink]:
    """Variant of ``_build_safety_for_test`` with KS#3 configured."""
    from yadirect_agent.agent.pipeline import SafetyPipeline
    from yadirect_agent.agent.plans import PendingPlansStore
    from yadirect_agent.agent.safety import (
        BudgetCapPolicy,
        ConversionIntegrityPolicy,
        MaxCpcPolicy,
        NegativeKeywordFloorPolicy,
        Policy,
        QueryDriftPolicy,
    )

    policy = Policy(
        budget_cap=BudgetCapPolicy(account_daily_budget_cap_rub=100_000),
        max_cpc=MaxCpcPolicy(),
        negative_keyword_floor=NegativeKeywordFloorPolicy(
            required_negative_keywords=required,
        ),
        query_drift=QueryDriftPolicy(),
        conversion_integrity=ConversionIntegrityPolicy(
            min_conversions_total=1,
            min_ratio_vs_baseline=0.5,
            require_all_baseline_goals_present=True,
        ),
        rollout_stage="autonomy_full",
    )
    return (
        SafetyPipeline(policy),
        PendingPlansStore(tmp_path / "pending_plans.jsonl"),
        _CapturingSink(),
    )


@pytest.mark.asyncio
async def test_resume_context_records_baseline_timestamp(
    settings: Settings, fake_direct: _FakeDirectService, tmp_path: Any
) -> None:
    """Auditor M2-ks3-negatives HIGH-2: ``ReviewContext.baseline_timestamp``
    must be stamped at snapshot-read time so the audit sink and any
    future apply-plan staleness check have a reference point.
    Without this, an apply-plan re-review minutes / hours after the
    plan was created has no signal that the underlying snapshot is
    stale — same gap closed for the bid context in the previous PR.
    """
    from datetime import UTC, datetime, timedelta

    from yadirect_agent.services.campaigns import _build_resume_context

    fake_direct._campaigns = [
        Campaign(
            Id=1,
            Name="alpha",
            State=CampaignState.SUSPENDED,
            Status=CampaignStatus.ACCEPTED,
            DailyBudget=DailyBudget(amount=500_000_000, mode="STANDARD"),
        )
    ]
    pipeline, store, sink = _build_safety_for_test(tmp_path)
    svc = CampaignService(settings, pipeline=pipeline, store=store, audit_sink=sink)

    before = datetime.now(UTC)
    ctx = await _build_resume_context(svc, [1])
    after = datetime.now(UTC)

    assert ctx.baseline_timestamp is not None
    assert ctx.baseline_timestamp.tzinfo is not None  # tz-aware, not naive
    assert before - timedelta(seconds=1) <= ctx.baseline_timestamp <= after + timedelta(seconds=1)


@pytest.mark.asyncio
async def test_pause_context_records_baseline_timestamp(
    settings: Settings, fake_direct: _FakeDirectService, tmp_path: Any
) -> None:
    """Same freshness signal must apply to pause / set_daily_budget —
    KS#1 (budget cap) on those paths reads the snapshot's daily
    budget totals, and a stale snapshot would let a parallel-operator
    bump slip through the cap arithmetic."""
    from datetime import UTC, datetime, timedelta

    from yadirect_agent.services.campaigns import _build_pause_context

    fake_direct._campaigns = [
        Campaign(
            Id=1,
            Name="alpha",
            State=CampaignState.ON,
            Status=CampaignStatus.ACCEPTED,
            DailyBudget=DailyBudget(amount=500_000_000, mode="STANDARD"),
        )
    ]
    pipeline, store, sink = _build_safety_for_test(tmp_path)
    svc = CampaignService(settings, pipeline=pipeline, store=store, audit_sink=sink)

    before = datetime.now(UTC)
    ctx = await _build_pause_context(svc, [1])
    after = datetime.now(UTC)

    assert ctx.baseline_timestamp is not None
    assert before - timedelta(seconds=1) <= ctx.baseline_timestamp <= after + timedelta(seconds=1)


@pytest.mark.asyncio
async def test_set_budget_context_records_baseline_timestamp(
    settings: Settings, fake_direct: _FakeDirectService, tmp_path: Any
) -> None:
    """Same freshness signal must apply to set_daily_budget."""
    from datetime import UTC, datetime, timedelta

    from yadirect_agent.services.campaigns import _build_set_budget_context

    fake_direct._campaigns = [
        Campaign(
            Id=1,
            Name="alpha",
            State=CampaignState.ON,
            Status=CampaignStatus.ACCEPTED,
            DailyBudget=DailyBudget(amount=500_000_000, mode="STANDARD"),
        )
    ]
    pipeline, store, sink = _build_safety_for_test(tmp_path)
    svc = CampaignService(settings, pipeline=pipeline, store=store, audit_sink=sink)

    before = datetime.now(UTC)
    ctx = await _build_set_budget_context(svc, 1, 800)
    after = datetime.now(UTC)

    assert ctx.baseline_timestamp is not None
    assert before - timedelta(seconds=1) <= ctx.baseline_timestamp <= after + timedelta(seconds=1)


@pytest.mark.asyncio
async def test_resume_blocks_when_campaign_missing_required_negatives(
    settings: Settings, fake_direct: _FakeDirectService, tmp_path: Any
) -> None:
    """KS#3: an operator-configured ``required_negative_keywords``
    that is NOT in the campaign's negatives → ``PlanRejected`` at
    plan-creation time. Before this PR the snapshot's
    ``negative_keywords`` was always empty so KS#3 blocked on every
    resume regardless of the actual campaign state. The test pins
    that resume is only blocked when the gap is real."""
    from yadirect_agent.agent.executor import PlanRejected

    fake_direct._campaigns = [
        Campaign(
            Id=1,
            Name="alpha",
            State=CampaignState.SUSPENDED,
            Status=CampaignStatus.ACCEPTED,
            DailyBudget=DailyBudget(amount=500_000_000, mode="STANDARD"),
            # Carries one of the two required phrases — still
            # missing the other → KS#3 must block.
            negative_keywords=["отзывы"],
        )
    ]
    pipeline, store, sink = _build_safety_with_required_negatives(
        tmp_path, required=["бесплатно", "отзывы"]
    )
    svc = CampaignService(settings, pipeline=pipeline, store=store, audit_sink=sink)

    with pytest.raises(PlanRejected):
        await svc.resume([1], rationale=_test_rationale(action="resume_campaigns"))

    # No mutation reached the API.
    assert fake_direct.resume_calls == []


@pytest.mark.asyncio
async def test_resume_proceeds_when_campaign_carries_all_required_negatives(
    settings: Settings, fake_direct: _FakeDirectService, tmp_path: Any
) -> None:
    """Inverse pin: a campaign that carries every required phrase
    proceeds to the confirm path (``auto_approve_resume=False``) —
    KS#3 must not fire on a compliant campaign. Before this PR the
    snapshot was always empty so KS#3 fired on EVERY resume the
    moment any required negative was configured; this test would
    have been impossible to pass."""
    from yadirect_agent.agent.executor import PlanRequired

    fake_direct._campaigns = [
        Campaign(
            Id=1,
            Name="alpha",
            State=CampaignState.SUSPENDED,
            Status=CampaignStatus.ACCEPTED,
            DailyBudget=DailyBudget(amount=500_000_000, mode="STANDARD"),
            negative_keywords=["бесплатно", "отзывы", "купить"],
        )
    ]
    pipeline, store, sink = _build_safety_with_required_negatives(
        tmp_path, required=["бесплатно", "отзывы"]
    )
    svc = CampaignService(settings, pipeline=pipeline, store=store, audit_sink=sink)

    with pytest.raises(PlanRequired):
        await svc.resume([1], rationale=_test_rationale(action="resume_campaigns"))

    # Plan persisted but DirectService NOT called (confirm tier).
    assert fake_direct.resume_calls == []
