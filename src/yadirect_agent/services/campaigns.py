"""Campaign management service.

All the 'smart' operations go through here — they combine multiple API
calls, validate preconditions, and emit audit events.

Mutating methods (currently ``set_daily_budget``) are wrapped with
``@requires_plan`` (M2.2 part 3). When a service is constructed with a
``SafetyPipeline`` and ``PendingPlansStore``, the decorator routes every
non-bypass call through the pipeline:

- ``allow``   → method runs, ``pipeline.on_applied`` fires.
- ``confirm`` → ``OperationPlan`` is appended to the store, the method
  raises ``PlanRequired``, the operator runs ``apply-plan <id>`` later.
- ``reject``  → method raises ``PlanRejected``.

A service constructed without ``pipeline``/``store`` cannot dispatch
mutating methods at all (``_resolve_safety`` raises ``RuntimeError``)
unless the caller passes ``_applying_plan_id`` to bypass — that path is
reserved for the apply-plan executor's re-entry. Read-only methods
(``list_active``, ``list_all``) remain unaffected.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from ..agent.executor import requires_plan
from ..agent.pipeline import ReviewContext, SafetyPipeline
from ..agent.plans import PendingPlansStore
from ..agent.safety import AccountBudgetSnapshot, BudgetChange, CampaignBudget
from ..audit import Actor, AuditSink, audit_action
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


async def _build_set_budget_context(
    service: CampaignService, campaign_id: int, budget_rub: int
) -> ReviewContext:
    """Async context builder for ``set_daily_budget``'s ``@requires_plan``.

    Reads the current account snapshot via ``list_all()`` and converts
    each ``CampaignSummary`` to a ``CampaignBudget`` for KS#1 (budget
    cap) and KS#3 (negative-keyword floor). Campaigns whose
    ``daily_budget_rub`` is ``None`` (no budget set) get ``0.0`` so they
    contribute nothing to the KS#1 sum but stay in the snapshot's
    ``campaigns`` list — KS#3 is stateless w.r.t. budget but cares
    about per-campaign presence.

    The single ``BudgetChange`` records the operator's intent so KS#1
    can compare ``proposed_total`` to ``account_daily_budget_cap_rub``.
    """
    # Read snapshot — ``await``s an HTTP round-trip in production;
    # tests stub via the DirectService monkeypatch fixture.
    summaries = await service.list_all()

    campaigns = [
        CampaignBudget(
            id=s.id,
            name=s.name,
            daily_budget_rub=0.0 if s.daily_budget_rub is None else s.daily_budget_rub,
            state=s.state,
        )
        for s in summaries
    ]
    snapshot = AccountBudgetSnapshot(campaigns=campaigns)

    return ReviewContext(
        budget_snapshot=snapshot,
        budget_changes=[
            BudgetChange(campaign_id=campaign_id, new_daily_budget_rub=budget_rub),
        ],
    )


class CampaignService:
    def __init__(
        self,
        settings: Settings,
        *,
        pipeline: SafetyPipeline | None = None,
        store: PendingPlansStore | None = None,
        audit_sink: AuditSink | None = None,
    ) -> None:
        """Build a CampaignService.

        ``pipeline`` / ``store`` / ``audit_sink`` are optional keyword-only:

        - Read-only call paths (``list_active`` / ``list_all``) work
          regardless — they don't touch the safety pipeline or audit.
        - Mutating methods (``set_daily_budget``) require pipeline+store.
          Calling one without the pair set raises ``RuntimeError`` from
          the decorator's ``_resolve_safety`` check, unless the caller
          passes ``_applying_plan_id`` (the apply-plan re-entry escape).
        - ``audit_sink`` is opt-in: if present, the mutating method
          wraps its API call in ``audit_action`` and emits
          ``set_campaign_budget.requested|.ok|.failed`` events. If
          absent, the method runs as-is (backwards compat for tests
          that don't thread a sink through). ``build_default_registry``
          always supplies one in production.

        The trio is typically built once per agent process (so the
        session TOCTOU register persists across tool calls within one
        run, and audit events from the same run share a JSONL file)
        and shared across services.
        """

        self._settings = settings
        self._pipeline = pipeline
        self._plans_store = store
        self._audit_sink = audit_sink
        self._logger = structlog.get_logger().bind(component="campaign_service")

    def _resolve_safety(self) -> tuple[SafetyPipeline, PendingPlansStore]:
        """Hand the (pipeline, store) pair to ``@requires_plan``.

        Raises ``RuntimeError`` rather than silently bypassing — silent
        fallback would let an agent call a mutating method without any
        safety checks, defeating the entire M2 layer.
        """
        if self._pipeline is None or self._plans_store is None:
            msg = (
                "CampaignService was constructed without a SafetyPipeline / "
                "PendingPlansStore; mutating methods cannot run. Build the "
                "service via build_default_registry (which wires the shared "
                "pipeline + store) or pass pipeline=... store=... explicitly."
            )
            raise RuntimeError(msg)
        return self._pipeline, self._plans_store

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
        # NB(M2): not yet wired through @requires_plan. Pause is fully
        # reversible (the agent can always resume) so the spending-risk
        # is bounded, but the rollout-stage gate, forbidden_operations
        # check, and the §M2.1 ``auto_approve_pause`` knob are dead
        # code on this path until decoration lands. Tracked in
        # docs/BACKLOG.md "M2 mutating methods awaiting @requires_plan".
        self._logger.info("campaigns.pause.request", ids=campaign_ids)
        async with DirectService(self._settings) as api:
            await api.suspend_campaigns(campaign_ids)
        self._logger.info("campaigns.pause.ok", ids=campaign_ids)

    async def resume(self, campaign_ids: list[int]) -> None:
        # NB(M2): same status as pause. Resume STARTS spending and is
        # the primary trigger for KS#3 (negative-keyword floor) per
        # the safety-spec; not yet @requires_plan-gated. Tracked in
        # docs/BACKLOG.md "M2 mutating methods awaiting @requires_plan".
        self._logger.info("campaigns.resume.request", ids=campaign_ids)
        async with DirectService(self._settings) as api:
            await api.resume_campaigns(campaign_ids)
        self._logger.info("campaigns.resume.ok", ids=campaign_ids)

    @requires_plan(
        action="set_campaign_budget",
        resource_type="campaign",
        preview_builder=lambda self, campaign_id, budget_rub: (
            f"set daily budget on campaign {campaign_id} to {budget_rub} RUB"
        ),
        context_builder=_build_set_budget_context,
        resource_ids_from_args=lambda self, campaign_id, budget_rub: [campaign_id],
    )
    async def set_daily_budget(self, campaign_id: int, budget_rub: int) -> None:
        """Single-campaign budget update. For bulk, batch at the service level.

        Wrapped by ``@requires_plan``: every call goes through the
        SafetyPipeline before reaching DirectService. The current policy
        has no ``auto_approve_budget_change`` knob, so every mutation
        returns ``confirm`` and persists an OperationPlan; the operator
        runs ``apply-plan <id>`` to actually send the request.

        Audit emission (when ``audit_sink`` is configured): emits
        ``set_campaign_budget.requested`` before the API call and
        ``set_campaign_budget.ok|.failed`` after. The ``actor`` field
        is determined by call shape — ``human`` when invoked through
        apply-plan (``_applying_plan_id`` kwarg present, intercepted
        by the decorator before this method runs), ``agent`` otherwise.

        The bypass kwarg ``_applying_plan_id`` (consumed by the
        decorator) is documented in
        ``yadirect_agent.agent.executor.requires_plan``.
        """
        if budget_rub < 300:
            # Direct's minimum is 300 RUB. Catching early saves a round-trip.
            msg = f"Daily budget must be >= 300 RUB, got {budget_rub}"
            raise ValueError(msg)

        # Actor determined by whether we got here via apply-plan
        # bypass (the decorator strips ``_applying_plan_id`` before
        # invoking us; presence of ``_apply_plan_caller`` signals the
        # bypass path). We look at the inspect frame for it.
        actor = self._infer_actor()

        if self._audit_sink is None:
            await self._do_set_daily_budget(campaign_id, budget_rub)
            return

        async with audit_action(
            self._audit_sink,
            actor=actor,
            action="set_campaign_budget",
            resource=f"campaign:{campaign_id}",
            args={"campaign_id": campaign_id, "budget_rub": budget_rub},
        ) as ctx:
            await self._do_set_daily_budget(campaign_id, budget_rub)
            ctx.set_result(
                {
                    "status": "applied",
                    "campaign_id": campaign_id,
                    "budget_rub": budget_rub,
                }
            )

    async def _do_set_daily_budget(self, campaign_id: int, budget_rub: int) -> None:
        """Inner API call — extracted so the audit_action wrapper has
        a single try/await/result-set point of contact."""
        self._logger.info(
            "campaigns.budget.request", campaign_id=campaign_id, budget_rub=budget_rub
        )
        async with DirectService(self._settings) as api:
            await api.update_campaign_budget(campaign_id, budget_rub)
        self._logger.info("campaigns.budget.ok", campaign_id=campaign_id)

    def _infer_actor(self) -> Actor:
        """Walk the caller frames to find ``_applying_plan_id`` — when
        the @requires_plan decorator's bypass branch runs, that kwarg
        is in the wrapper's local frame; if we find it, the actor is
        the operator running apply-plan, not the agent.

        Frame inspection is ugly but the alternative is plumbing an
        ``actor`` argument all the way from the executor through the
        decorator into the wrapped method, which fights the decorator's
        signature-transparency contract. The frame walk is bounded
        (we look ~5 frames up and stop).
        """
        import sys
        from types import FrameType

        frame: FrameType | None = sys._getframe(1)  # caller of set_daily_budget
        for _ in range(5):
            if frame is None:
                break
            if frame.f_locals.get("_applying_plan_id") is not None:
                return "human"
            frame = frame.f_back
        return "agent"
