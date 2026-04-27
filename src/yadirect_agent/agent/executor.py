"""@requires_plan decorator + apply_plan executor (M2.2 part 3a).

This module is the glue between the pipeline (``SafetyPipeline``),
the plan store (``PendingPlansStore``), and concrete service methods
that want to mutate Direct. It is deliberately agnostic of any specific
service class — the decorator resolves the pipeline and store via a
``_resolve_safety() -> (pipeline, store)`` method that the decorated
class must implement. PR B wires ``CampaignService`` against this
contract.

Flow (``@requires_plan`` path):

    agent → service.set_daily_budget(1, 200)
         → decorator builds OperationPlan + ReviewContext
         → pipeline.review(plan, ctx) → SafetyDecision
         → allow:   wrapped method runs; pipeline.on_applied(ctx)
            confirm: plan appended to store; PlanRequired raised
            reject:  PlanRejected raised; nothing persisted

Flow (``apply_plan`` path — operator confirming a pending plan):

    operator → yadirect-agent apply-plan <id>
             → apply_plan(id)
             → store.get(id); validate status == pending
             → deserialize_review_context(plan.review_context)
             → pipeline.review(plan, ctx)  [re-review against original ctx]
             → reject:  store.update_status(id, rejected); raise
                allow/confirm: service_router(action, args, _applying_plan_id=id)
                              → decorator bypass (sees _applying_plan_id)
                              → wrapped method runs
             → success: store pending→applied; pipeline.on_applied(ctx)
                        (best-effort — see below)
             → exception: store pending→failed; DO NOT call on_applied
                          (propagate error to operator)

Ordering of ``store.update_status("applied")`` BEFORE
``pipeline.on_applied(ctx)`` on the success path is deliberate
(auditor C-1). The API call has spent real money; the plan record
must reflect that immediately so a crash here cannot leave the plan
in ``pending`` and let a second ``apply-plan`` double-spend.
``on_applied`` is best-effort post-success — if it raises, the
session TOCTOU register loses one entry (defendable degradation:
the next plan goes through normal pipeline checks) but the store
correctly reflects the API write.

The ``on_applied`` contract on the failure path is a hard invariant
— if the executor raises, the session TOCTOU register must not
record the approved bid. Tested in ``TestApplyPlanExecutorFailure``
and ``TestApplyPlanOnAppliedRobustness``.
"""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Protocol

from ..audit import AuditSink, audit_action
from .pipeline import (
    ReviewContext,
    SafetyPipeline,
    deserialize_review_context,
    serialize_review_context,
)
from .plans import OperationPlan, PendingPlansStore, generate_plan_id
from .safety import CheckResult

__all__ = [
    "InvalidPlanStateError",
    "PlanRejected",
    "PlanRequired",
    "apply_plan",
    "requires_plan",
]


# --------------------------------------------------------------------------
# Exceptions.
# --------------------------------------------------------------------------


class PlanRequired(Exception):  # noqa: N818 — name signals "operator action required", not a runtime error
    """Raised by ``@requires_plan`` when the pipeline returns ``confirm``.

    The wrapped service method did NOT run. The plan has been
    persisted to ``PendingPlansStore``; the operator can run
    ``yadirect-agent apply-plan <plan_id>`` to confirm.
    """

    def __init__(self, plan_id: str, preview: str, reason: str) -> None:
        super().__init__(f"plan {plan_id} requires confirmation: {reason}")
        self.plan_id = plan_id
        self.preview = preview
        self.reason = reason


class PlanRejected(Exception):  # noqa: N818 — symmetric with PlanRequired; semantic name beats "Error"
    """Raised by ``@requires_plan`` and ``apply_plan`` when the pipeline
    returns ``reject``.

    ``blocking`` is the raw ``list[CheckResult]`` from the
    ``SafetyDecision.blocking_checks`` — preserved verbatim so the
    caller (CLI, audit sink) can show ``reason`` and ``details`` from
    each blocking check. Never persisted to the plan store — the M2.3
    audit sink will log every decision; the plan store is for plans
    waiting on a decision.
    """

    def __init__(self, reason: str, blocking: list[CheckResult]) -> None:
        super().__init__(reason)
        self.reason = reason
        self.blocking = blocking


class InvalidPlanStateError(Exception):
    """Raised by ``apply_plan`` when the plan's current status cannot
    transition to ``applied`` (already applied / rejected / failed).
    """


class StaleSnapshotError(Exception):
    """Raised by ``apply_plan`` when the plan's stored snapshot is
    older than ``Policy.max_snapshot_age_seconds``.

    KS#1 cap arithmetic and KS#4's ``_is_increase`` both compare
    proposed values against the snapshot baseline. An apply-plan
    re-review against an arbitrarily-old snapshot would be invisible
    to a parallel-operator change between plan creation and apply
    execution, opening a window for a second consecutive bid bump
    or cap-skirting move. The plan transitions to ``failed``
    (terminal); the operator re-issues the plan against a fresh
    snapshot.

    Attributes:
        age_seconds: how stale the snapshot was at the moment of
            the check (float; sub-second precision retained for
            audit logging).
        max_age_seconds: the configured ceiling.
    """

    def __init__(self, plan_id: str, age_seconds: float, max_age_seconds: int) -> None:
        super().__init__(
            f"plan {plan_id} snapshot is {age_seconds:.1f}s old, "
            f"exceeds max_snapshot_age_seconds={max_age_seconds}"
        )
        self.age_seconds = age_seconds
        self.max_age_seconds = max_age_seconds


# --------------------------------------------------------------------------
# Decorated-service contract.
# --------------------------------------------------------------------------


class _SafetyAware(Protocol):
    """A service class that uses ``@requires_plan`` must implement this.

    Keeping it explicit (rather than digging into private attributes)
    means the decorator has zero knowledge of specific service classes.
    PR B's ``CampaignService`` will expose a matching method.
    """

    def _resolve_safety(self) -> tuple[SafetyPipeline, PendingPlansStore]: ...


# --------------------------------------------------------------------------
# @requires_plan decorator.
# --------------------------------------------------------------------------


def requires_plan(
    *,
    action: str,
    resource_type: str,
    preview_builder: Callable[..., str],
    context_builder: Callable[..., Awaitable[ReviewContext]],
    resource_ids_from_args: Callable[..., list[int]],
) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    """Wrap an async service method in the plan→confirm→execute flow.

    Arguments (all keyword-only to keep call sites readable):

    - ``action``: ``OperationPlan.action`` — e.g. ``"set_campaign_budget"``.
      Must be stable across versions; the re-apply path looks up the
      service method by this name (via the ``service_router`` the CLI
      builds).
    - ``resource_type``: ``OperationPlan.resource_type`` — e.g.
      ``"campaign"``. Free-form today; the audit sink (M2.3) will
      classify by this.
    - ``preview_builder(self, *args, **kwargs) -> str``: one-line
      human-readable summary for ``plans list``. Keep it specific
      (include ids and values).
    - ``context_builder(self, *args, **kwargs) -> Awaitable[ReviewContext]``:
      async — real-world builders read snapshots from the Direct API
      (e.g. ``CampaignService.list_all()``) and that's an `await` away.
      Tests pass an ``async def`` returning a pre-built context.
    - ``resource_ids_from_args(self, *args, **kwargs) -> list[int]``:
      primary ids touched by this call. Used by the CLI/audit for
      cross-referencing.

    The wrapped function picks up a ``_applying_plan_id`` kwarg used by
    ``apply_plan`` to bypass the pipeline on re-entry (apply_plan
    already re-reviewed; decorator must not double-review).
    """

    def decorator(
        fn: Callable[..., Awaitable[Any]],
    ) -> Callable[..., Awaitable[Any]]:
        @functools.wraps(fn)
        async def wrapper(
            self: _SafetyAware,
            *args: Any,
            _applying_plan_id: str | None = None,
            **kwargs: Any,
        ) -> Any:
            # Bypass path: apply_plan already handled review + will
            # handle on_applied. Just run the wrapped call.
            if _applying_plan_id is not None:
                return await fn(self, *args, **kwargs)

            pipeline, store = self._resolve_safety()
            context = await context_builder(self, *args, **kwargs)

            # Build a provisional plan. ``reason`` is filled from the
            # decision below before persistence.
            plan = OperationPlan(
                plan_id=generate_plan_id(),
                created_at=datetime.now(UTC),
                action=action,
                resource_type=resource_type,
                resource_ids=resource_ids_from_args(self, *args, **kwargs),
                args=_bound_args_dict(fn, args, kwargs),
                preview=preview_builder(self, *args, **kwargs),
                reason="awaiting pipeline decision",
                review_context=serialize_review_context(context),
            )

            decision = pipeline.review(plan, context)

            if decision.status == "reject":
                raise PlanRejected(decision.reason or "rejected", list(decision.blocking_checks))

            if decision.status == "confirm":
                persisted = plan.model_copy(
                    update={
                        "reason": decision.reason or "awaiting operator confirmation",
                    }
                )
                store.append(persisted)
                raise PlanRequired(persisted.plan_id, persisted.preview, persisted.reason)

            # allow: execute, then record session state.
            result = await fn(self, *args, **kwargs)
            pipeline.on_applied(context)
            return result

        return wrapper

    return decorator


def _bound_args_dict(
    fn: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]
) -> dict[str, Any]:
    """Serialise the method's positional + keyword args into a dict.

    Uses ``inspect.Signature.bind`` so:
    - The dict keys match the method's parameter names (e.g.
      ``{"campaign_id": 1, "new_budget_rub": 200}``).
    - Excess positional arguments raise ``TypeError`` instead of
      being silently dropped (auditor H-1).
    - ``*args`` tuples are preserved as tuples (auditor H-1).
    - Defaulted parameters omitted by the caller are filled in
      via ``apply_defaults()`` so the on-disk plan reflects the
      *exact* call shape and apply-plan replays it identically
      across deployments where a default may have changed
      (auditor M-2).

    ``self`` is stripped — positional args come in after the
    instance method bound it. The bypass kwarg ``_applying_plan_id``
    is filtered before binding so it never reaches the wrapped
    function's signature.
    """
    import inspect

    sig = inspect.signature(fn)
    # Drop the ``self`` param from the signature — the decorator
    # handles it separately, and ``args``/``kwargs`` here do not
    # contain it.
    params = [p for p in sig.parameters.values() if p.name != "self"]
    new_sig = sig.replace(parameters=params)

    filtered_kwargs = {k: v for k, v in kwargs.items() if k != "_applying_plan_id"}

    # ``bind`` raises TypeError on arity / unknown-kwarg mismatch —
    # we want that surface immediately rather than producing a
    # plan that can't be faithfully replayed later.
    bound = new_sig.bind(*args, **filtered_kwargs)
    bound.apply_defaults()
    return dict(bound.arguments)


# --------------------------------------------------------------------------
# apply_plan executor.
# --------------------------------------------------------------------------


# service_router contract: takes the plan's action + args (plus the
# _applying_plan_id escape hatch) and returns whatever the underlying
# service method returns. The CLI (PR B) builds this router over the
# concrete service instances.
_ServiceRouter = Callable[..., Awaitable[Any]]


async def apply_plan(
    plan_id: str,
    *,
    store: PendingPlansStore,
    pipeline: SafetyPipeline,
    service_router: _ServiceRouter,
    audit_sink: AuditSink | None = None,
) -> Any:
    """Apply a pending operation plan.

    Invariants (tested in ``test_executor.py``):

    1. ``plan.status`` must be ``pending`` on entry. Any other status
       is ``InvalidPlanStateError`` — applied / rejected / failed are
       terminal; ``approved`` is legacy / unused for now.
    2. ``plan.review_context`` must be non-null. Apply-plan re-reviews
       the plan against the original snapshot before executing, so a
       plan without a stored context cannot be applied.
    3. Re-review runs exactly once before the executor. A ``reject``
       here updates the store to ``rejected`` (catches snapshot drift)
       and propagates ``PlanRejected`` without touching the executor.
    4. If the executor raises, ``pipeline.on_applied`` is NOT called
       and the store is updated to ``failed``. This is a hard
       invariant: the session TOCTOU register must never reflect an
       API call that didn't succeed.
    5. On success, ``pipeline.on_applied`` is called exactly once and
       the store moves ``pending → applied``.

    Audit emission (when ``audit_sink`` is configured): emits
    ``apply_plan.requested`` on entry and ``apply_plan.ok|.failed``
    on exit, all with ``actor="human"`` and ``resource="plan:<id>"``.
    The .failed event fires for both re-review reject AND executor
    raise — operators see the attempt regardless of how it ended.
    Sink absent → no events emitted (backwards compat).
    """

    if audit_sink is None:
        return await _apply_plan_inner(plan_id, store, pipeline, service_router)

    async with audit_action(
        audit_sink,
        actor="human",
        action="apply_plan",
        resource=f"plan:{plan_id}",
        args={"plan_id": plan_id},
    ) as ctx:
        result = await _apply_plan_inner(plan_id, store, pipeline, service_router)
        ctx.set_result({"status": "applied", "plan_id": plan_id})
        return result


async def _apply_plan_inner(
    plan_id: str,
    store: PendingPlansStore,
    pipeline: SafetyPipeline,
    service_router: _ServiceRouter,
) -> Any:
    """Inner apply-plan body, factored out so ``audit_action`` wraps a
    single block. No behaviour change vs M2.2."""

    plan = store.get(plan_id)
    if plan is None:
        msg = f"plan not found: {plan_id!r}"
        raise KeyError(msg)

    if plan.status != "pending":
        msg = f"plan {plan_id} is {plan.status!r}, cannot apply"
        raise InvalidPlanStateError(msg)

    if plan.review_context is None:
        msg = f"plan {plan_id} has no stored review_context; cannot re-review"
        raise InvalidPlanStateError(msg)

    context = deserialize_review_context(plan.review_context)

    # Snapshot-freshness gate: a plan whose baseline_timestamp is
    # older than the policy ceiling fails terminally. ``None`` is
    # fail-open ("no signal, can't prove staleness") to keep legacy
    # plans that predate the timestamp rollout (or context builders
    # that don't yet stamp it) applicable. Auditor M2-bid-snapshot /
    # M2-ks3-negatives HIGH-2 follow-up.
    if context.baseline_timestamp is not None:
        # Clamp negative age (future timestamp from NTP jitter or a
        # corrupt / malicious JSONL row) to zero so the staleness
        # gate cannot be trivially bypassed by writing a far-future
        # ``baseline_timestamp``: ``(now - future).total_seconds()``
        # is negative and ``negative > max_age`` is False without
        # the clamp. Auditor M2-snapshot-age second-pass.
        age = max(0.0, (datetime.now(UTC) - context.baseline_timestamp).total_seconds())
        max_age = pipeline.policy.max_snapshot_age_seconds
        if age > max_age:
            store.update_status(plan_id, "failed")
            raise StaleSnapshotError(plan_id, age, max_age)

    decision = pipeline.review(plan, context)

    if decision.status == "reject":
        store.update_status(plan_id, "rejected")
        raise PlanRejected(decision.reason or "re-review rejected", list(decision.blocking_checks))

    # For ``allow`` and ``confirm`` at re-review time we proceed:
    # the operator explicitly asked to apply via apply-plan, which
    # IS the confirmation for confirm-tier plans. reject is the only
    # status that blocks execution at this stage.

    try:
        result = await service_router(plan.action, plan.args, _applying_plan_id=plan_id)
    except Exception:
        # Executor failed — do NOT record session state, and DO NOT
        # mark the plan applied. ``failed`` is terminal; subsequent
        # apply-plan attempts will surface InvalidPlanStateError.
        store.update_status(plan_id, "failed")
        raise

    # CRITICAL ordering (auditor C-1): the API call has succeeded —
    # money has been spent. We MUST mark the plan ``applied`` before
    # any further work that could raise, so that a crash here cannot
    # leave the plan in ``pending`` and let a second ``apply-plan``
    # double-spend. ``on_applied`` runs second; if it raises, the
    # session TOCTOU register loses one entry (defendable degradation
    # — the next plan goes through normal pipeline checks) but the
    # store correctly reflects that the API write happened.
    store.update_status(plan_id, "applied")
    try:
        pipeline.on_applied(context)
    except Exception:
        # Don't shadow the executor's success: log via stdlib (the
        # session register update is best-effort once the API write
        # has succeeded). Re-raising would mislead the caller into
        # thinking the underlying operation failed.
        import logging

        logging.getLogger(__name__).exception(
            "pipeline.on_applied raised after successful apply for plan %s; "
            "store marked applied; session TOCTOU register may be stale",
            plan_id,
        )
    return result
