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
from typing import Any

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
    """Flattened view for agent consumption — no nested micro-currency fiddling.

    Deliberately does NOT carry ``negative_keywords`` — those are
    operator-configured business intent (brand misspells / competitor
    names / etc.) and stay on the safety-internal path. The bid
    snapshot builder reads them via ``DirectService.get_campaigns``
    directly, not through this summary, so they never reach the
    agent's ``list_campaigns`` tool response or the CLI ``--json``
    output.
    """

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


class PartialActionError(RuntimeError):
    """Raised when Yandex Direct's bulk-action response contains
    per-item errors despite the top-level call returning HTTP 200.

    Direct's ``campaigns.suspend`` / ``campaigns.resume`` /
    ``campaigns.archive`` return an ``ActionResults`` envelope
    where each element can carry per-item ``Errors``. The top-
    level error path doesn't fire on partial success — the
    response says "OK" while two of three campaigns weren't
    touched. Without this exception the plan would silently
    transition to ``applied`` while the operator's intent was
    only partially fulfilled, breaking the bulk plan's
    "all-or-none" contract. Auditor M2 follow-up MEDIUM.
    """

    def __init__(self, action: str, failed_items: list[dict[str, Any]]) -> None:
        super().__init__(f"{action}: {len(failed_items)} of N items failed at the API level")
        self.action = action
        self.failed_items = failed_items


def _assert_action_results_clean(result: dict[str, Any], results_key: str, action: str) -> None:
    """Inspect ``result[results_key]`` for per-item ``Errors``.

    If any element carries a non-empty ``Errors`` list, raise
    ``PartialActionError`` so the caller can transition the plan
    to ``failed`` rather than silently log success on partial
    completion.

    Tolerates a missing ``results_key`` (returns clean) — that
    happens for the empty-id-list case and in fakes that don't
    yet emit the envelope shape.
    """
    items = result.get(results_key, [])
    if not isinstance(items, list):
        return
    failed = [item for item in items if isinstance(item, dict) and item.get("Errors")]
    if failed:
        raise PartialActionError(action, failed)


async def _build_account_budget_snapshot(
    service: CampaignService,
) -> AccountBudgetSnapshot:
    """Read the current ``AccountBudgetSnapshot`` from Direct.

    Shared helper used by every ``CampaignService`` method that
    needs the account state for KS#1 (budget cap) and KS#3
    (negative-keyword floor) evaluation. Bypasses the
    agent-facing ``CampaignSummary`` flattener and reads the wire
    ``Campaign`` model directly so per-campaign negatives flow
    through to ``CampaignBudget.negative_keywords`` without leaking
    operator-configured negatives to the agent's ``list_campaigns``
    tool response (same defence-in-depth pattern as
    ``_build_bid_context``).
    """

    async with DirectService(service._settings) as api:
        campaigns_raw = await api.get_campaigns()

    campaigns: list[CampaignBudget] = []
    for c in campaigns_raw:
        budget_rub: float = 0.0
        if c.daily_budget is not None:
            budget_rub = c.daily_budget.amount / 1_000_000
        campaigns.append(
            CampaignBudget(
                id=c.id,
                name=c.name,
                daily_budget_rub=budget_rub,
                state=c.state.value if c.state else "UNKNOWN",
                negative_keywords=frozenset(c.negative_keywords),
            )
        )
    return AccountBudgetSnapshot(campaigns=campaigns)


async def _build_pause_context(service: CampaignService, campaign_ids: list[int]) -> ReviewContext:
    """Context for ``pause(ids)``: each id transitions to SUSPENDED.

    KS#1 (budget cap) is satisfied trivially — pausing only LOWERS
    the active total. KS#3 (negative-keyword floor) does not run on
    pause (only on resume). The snapshot is included anyway so the
    pipeline's required-snapshot guard passes for the action.
    """

    snapshot = await _build_account_budget_snapshot(service)
    return ReviewContext(
        budget_snapshot=snapshot,
        budget_changes=[
            BudgetChange(campaign_id=cid, new_state="SUSPENDED") for cid in campaign_ids
        ],
    )


async def _build_resume_context(service: CampaignService, campaign_ids: list[int]) -> ReviewContext:
    """Context for ``resume(ids)``: each id transitions to ON.

    Resume is the primary KS#3 trigger per safety-spec — every
    resumed campaign must satisfy the configured
    ``required_negative_keywords`` floor. Snapshot's
    ``negative_keywords`` field is currently always empty (we don't
    yet read per-campaign negatives from the Direct API). Default
    policy ships with empty ``required_negative_keywords`` so KS#3
    is a no-op out of the box; once the operator configures
    required negatives in YAML, the next agent run will start
    blocking resume on every campaign until per-campaign negative
    fetch lands. Tracked in BACKLOG.
    """

    snapshot = await _build_account_budget_snapshot(service)
    return ReviewContext(
        budget_snapshot=snapshot,
        budget_changes=[BudgetChange(campaign_id=cid, new_state="ON") for cid in campaign_ids],
    )


async def _build_set_budget_context(
    service: CampaignService, campaign_id: int, budget_rub: int
) -> ReviewContext:
    """Async context builder for ``set_daily_budget``'s ``@requires_plan``.

    The single ``BudgetChange`` records the operator's intent so KS#1
    can compare ``proposed_total`` to ``account_daily_budget_cap_rub``.
    """
    snapshot = await _build_account_budget_snapshot(service)

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

    @requires_plan(
        action="pause_campaigns",
        resource_type="campaign",
        preview_builder=lambda self, campaign_ids: f"pause campaigns: {campaign_ids}",
        context_builder=_build_pause_context,
        resource_ids_from_args=lambda self, campaign_ids: list(campaign_ids),
    )
    async def pause(self, campaign_ids: list[int]) -> None:
        """Suspend one or more campaigns. Bulk operation: a single
        plan covers the whole list; apply-plan applies all-or-none.

        Wrapped by ``@requires_plan``. With ``Policy.auto_approve_pause=True``
        (default), the pipeline returns ``allow`` after KS#1 (budget
        cap, satisfied trivially since pause lowers the active total)
        and the approval tier — so pause completes in one shot
        through the agent path. Audit emits
        ``pause_campaigns.requested|.ok|.failed`` regardless.
        """
        if self._audit_sink is None:
            await self._do_pause(campaign_ids)
            return

        actor = self._infer_actor()
        async with audit_action(
            self._audit_sink,
            actor=actor,
            action="pause_campaigns",
            resource=f"campaigns:{campaign_ids}",
            args={"campaign_ids": campaign_ids},
        ) as ctx:
            await self._do_pause(campaign_ids)
            ctx.set_result({"status": "applied", "paused": campaign_ids})

    async def _do_pause(self, campaign_ids: list[int]) -> None:
        self._logger.info("campaigns.pause.request", ids=campaign_ids)
        async with DirectService(self._settings) as api:
            result = await api.suspend_campaigns(campaign_ids)
        # Direct returns HTTP 200 with per-item errors on partial
        # success; surface those as a service-level failure so the
        # plan transitions to ``failed`` rather than ``applied``.
        # Auditor M2 follow-up MEDIUM.
        _assert_action_results_clean(result, "SuspendResults", "pause_campaigns")
        self._logger.info("campaigns.pause.ok", ids=campaign_ids)

    @requires_plan(
        action="resume_campaigns",
        resource_type="campaign",
        preview_builder=lambda self, campaign_ids: f"resume campaigns: {campaign_ids}",
        context_builder=_build_resume_context,
        resource_ids_from_args=lambda self, campaign_ids: list(campaign_ids),
    )
    async def resume(self, campaign_ids: list[int]) -> None:
        """Un-suspend one or more campaigns. Bulk; one plan per call.

        Wrapped by ``@requires_plan``. Resume is the primary KS#3
        trigger per safety-spec — every resumed campaign must
        satisfy the configured ``required_negative_keywords`` floor.
        With ``Policy.auto_approve_resume=False`` (default), the
        pipeline returns ``confirm`` and the operator must run
        ``yadirect-agent apply-plan <id>`` to actually unsuspend.
        Audit emits ``resume_campaigns.requested|.ok|.failed``.
        """
        if self._audit_sink is None:
            await self._do_resume(campaign_ids)
            return

        actor = self._infer_actor()
        async with audit_action(
            self._audit_sink,
            actor=actor,
            action="resume_campaigns",
            resource=f"campaigns:{campaign_ids}",
            args={"campaign_ids": campaign_ids},
        ) as ctx:
            await self._do_resume(campaign_ids)
            ctx.set_result({"status": "applied", "resumed": campaign_ids})

    async def _do_resume(self, campaign_ids: list[int]) -> None:
        self._logger.info("campaigns.resume.request", ids=campaign_ids)
        async with DirectService(self._settings) as api:
            result = await api.resume_campaigns(campaign_ids)
        _assert_action_results_clean(result, "ResumeResults", "resume_campaigns")
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
            # NB: this validation raises BEFORE ``audit_action`` opens, so
            # no ``set_campaign_budget.*`` event fires. The apply_plan
            # outer envelope (when called via apply-plan) still emits
            # ``apply_plan.requested`` + ``apply_plan.failed`` carrying
            # this ValueError, so operator-visible audit isn't lost on
            # the apply path. Auditor PR M2.3b LOW.
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
        """Walk the caller frames to find the @requires_plan ``wrapper``
        with ``_applying_plan_id`` set — that's the only frame the
        decorator's bypass branch produces, and its presence means the
        operator drove this call via apply-plan.

        Auditor HIGH: the previous version checked ANY frame's locals
        for ``_applying_plan_id``, which made the actor classification
        sensitive to local-name collisions in middleware / orchestration
        / test code. Pinning the match to ``frame.f_code.co_name ==
        "wrapper"`` ensures only the canonical decorator frame qualifies.

        Frame inspection is ugly but the alternative is plumbing an
        ``actor`` argument from the executor through the decorator into
        the wrapped method, which fights the decorator's
        signature-transparency contract. The walk is bounded
        (we look ~8 frames up and stop) so unrelated frames don't
        affect the verdict.
        """
        import sys
        from types import FrameType

        # Start one frame above this helper (i.e. inside the caller
        # ``pause`` / ``resume`` / ``set_daily_budget``) and walk
        # outward looking for the @requires_plan decorator's wrapper
        # frame. The wrapper holds ``_applying_plan_id`` as a local
        # only on the apply-plan re-entry path.
        frame: FrameType | None = sys._getframe(1)
        for _ in range(8):
            if frame is None:
                break
            # Match only the decorator's wrapper closure — name is
            # set by ``functools.wraps(fn)`` in ``requires_plan`` to
            # ``"wrapper"`` before ``__wrapped__`` rewrites it. The
            # closure body holds ``_applying_plan_id`` as a local.
            if (
                frame.f_code.co_name == "wrapper"
                and frame.f_locals.get("_applying_plan_id") is not None
            ):
                return "human"
            frame = frame.f_back
        return "agent"
