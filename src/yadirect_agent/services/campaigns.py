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
    ) -> None:
        """Build a CampaignService.

        ``pipeline`` and ``store`` are optional and keyword-only:
        - Read-only call paths (``list_active`` / ``list_all``) work
          regardless — they don't touch the safety pipeline.
        - Mutating methods (``set_daily_budget``) require both. Calling
          one without the pair set raises ``RuntimeError`` from the
          decorator's ``_resolve_safety`` check, unless the caller
          passes ``_applying_plan_id`` (the apply-plan re-entry escape).

        The pipeline and store are typically built once per agent
        process (so the session TOCTOU register persists across tool
        calls within one agent run) and shared across services.
        """

        self._settings = settings
        self._pipeline = pipeline
        self._plans_store = store
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
        self._logger.info("campaigns.pause.request", ids=campaign_ids)
        async with DirectService(self._settings) as api:
            await api.suspend_campaigns(campaign_ids)
        self._logger.info("campaigns.pause.ok", ids=campaign_ids)

    async def resume(self, campaign_ids: list[int]) -> None:
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

        The bypass kwarg ``_applying_plan_id`` (consumed by the
        decorator) is documented in
        ``yadirect_agent.agent.executor.requires_plan``.
        """
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
