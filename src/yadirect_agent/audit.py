"""Audit sink: ``AuditEvent`` + ``JsonlSink`` + ``audit_action`` (M2.3a).

Every mutating service method emits ``<action>.requested`` before the
underlying API call and ``<action>.ok`` / ``<action>.failed`` after,
through an :class:`AuditSink`. The default sink writes one
JSON-Lines event per line to ``Settings.audit_log_path`` so a fresh
deployment has a tamper-evident trail of every decision the agent and
the operator made.

The protocol is deliberately narrow — ``async emit(event) -> None`` —
so a future deployment can swap the file sink for Kafka / Postgres /
S3 without touching service code. The ``audit_action`` async context
manager wraps both ends of an operation:

    async with audit_action(sink, actor="agent", action="set_campaign_budget",
                             args={"campaign_id": 42, "budget_rub": 800},
                             trace_id=trace_id) as ctx:
        result = await direct_api.update_campaign_budget(42, 800)
        ctx.set_result({"status": "applied", "campaign_id": 42})
        ctx.set_units_spent(direct_api.last_units)

Two events fire — ``set_campaign_budget.requested`` on entry,
``set_campaign_budget.ok`` (or ``.failed``) on exit. If an exception
is raised inside the block, the ``.failed`` event carries
``result["error_type"]`` and ``result["error_message"]`` alongside any
partial result the caller had already set, and the exception
propagates unchanged.

PII redaction is applied at the sink boundary via
:func:`redact_for_audit`. Today the only stripped key is
``new_queries_sample`` (raw KS#7 user search queries) — same blocklist
the tools-layer response redactor uses, intentional defence in depth.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType
from typing import Any, Literal, Protocol

import structlog
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

__all__ = [
    "Actor",
    "AuditEvent",
    "AuditSink",
    "JsonlSink",
    "audit_action",
    "redact_for_audit",
]


# --------------------------------------------------------------------------
# AuditEvent — the on-disk record shape.
# --------------------------------------------------------------------------


Actor = Literal["agent", "human", "system"]


# --------------------------------------------------------------------------
# Caller-frame actor inference.
#
# Shared between ``CampaignService`` and ``BiddingService``: when a
# mutating service method is invoked through ``apply_plan``, the
# decorator's bypass branch sets ``_applying_plan_id`` as a local in
# its ``wrapper`` closure. Walking the caller's frames for that exact
# closure pin distinguishes the operator-driven apply-plan path
# (actor = ``human``) from the agent's allow path (``agent``).
#
# Auditor M2-bidding L-1 motivation: extracting the helper means a
# future tightening (e.g. replacing the frame walk with explicit
# kwarg threading through the decorator) lands in one place, not
# duplicated across every ``@requires_plan``-aware service.
# --------------------------------------------------------------------------


_ACTOR_FRAME_WALK_DEPTH = 8


def infer_actor_from_frame() -> Actor:
    """Identify the actor of a ``@requires_plan``-decorated call by
    inspecting the caller's frame stack.

    Returns ``"human"`` if any frame within
    ``_ACTOR_FRAME_WALK_DEPTH`` of the immediate caller is named
    exactly ``wrapper`` AND has ``_applying_plan_id`` set in its
    locals. Returns ``"agent"`` otherwise.

    Pin tighter than ``_applying_plan_id in any frame's locals``:
    the decorator's wrapper closure is named exactly ``wrapper``,
    so we match only ``frame.f_code.co_name == "wrapper"``. Auditor
    HIGH from PR M2.2 part 3b1: the previous implementation flipped
    the verdict on local-name collisions in unrelated code (test
    fixtures, middleware) that happened to use ``_applying_plan_id``
    as a local variable for any reason.

    The 8-frame ceiling prevents runaway walks in deeply-nested
    middleware / orchestration / test code from wandering into
    arbitrary frames whose locals have nothing to do with the
    decorator.
    """
    frame: FrameType | None = sys._getframe(1)
    for _ in range(_ACTOR_FRAME_WALK_DEPTH):
        if frame is None:
            break
        if (
            frame.f_code.co_name == "wrapper"
            and frame.f_locals.get("_applying_plan_id") is not None
        ):
            return "human"
        frame = frame.f_back
    return "agent"


class AuditEvent(BaseModel):
    """One record in the audit log.

    Every mutating service-method invocation produces at least two
    events: ``<action>.requested`` and ``<action>.ok|.failed``. Reading
    the JSONL chronologically reconstructs the full timeline of who
    did what when, including failed attempts.

    Field semantics:
    - ``ts``: server-side wall clock at event creation. Always
      timezone-aware UTC.
    - ``trace_id``: ties events back to one agent turn / CLI invocation.
      ``None`` for events fired outside a request scope (e.g. boot).
    - ``actor``: who initiated. ``agent`` = the LLM via tool call,
      ``human`` = operator via apply-plan / direct CLI, ``system`` = the
      runtime itself (rollout-stage promotion, scheduled tasks).
    - ``action``: dotted name. The verb part is stable across versions
      (``set_campaign_budget``); the suffix is one of ``.requested``,
      ``.ok``, ``.failed``.
    - ``resource``: free-form identifier. Convention is
      ``"<type>:<id>"`` so ``"campaign:42"`` matches the operation plan's
      ``resource_type`` + ``resource_ids[0]``.
    - ``args``: the kwargs passed to the underlying service method.
      Already filtered of bypass kwargs (``_applying_plan_id``).
    - ``result``: ``None`` on the .requested event; a structured dict on
      .ok / .failed. ``.failed`` events ALWAYS include ``error_type``
      and ``error_message``; an caller-set partial result is preserved
      alongside.
    - ``units_spent``: optional Direct API points consumed. Useful for
      capacity planning; ``None`` when the call didn't touch Direct.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # ``AwareDatetime`` rejects naive datetimes — the audit log must
    # be sortable / comparable across timezones (auditor M-1).
    ts: AwareDatetime
    actor: Actor
    action: str = Field(..., min_length=1)
    trace_id: str | None = None
    resource: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] | None = None
    units_spent: int | None = None


# --------------------------------------------------------------------------
# Sink protocol + default JSONL implementation.
# --------------------------------------------------------------------------


class AuditSink(Protocol):
    """Anyone who can persist an :class:`AuditEvent` is a sink.

    Concrete implementations include :class:`JsonlSink` (default), and
    in-memory stubs in tests. Future implementations can ship over
    Kafka, Postgres, etc. without touching service code.
    """

    async def emit(self, event: AuditEvent) -> None: ...


# Privacy: keys we MUST strip from any ``args`` / ``result`` dict
# before persisting. Today's entries:
# - ``new_queries_sample``: KS#7 (query drift) raw user search query
#   sample. Same blocklist the tools-layer response redactor uses
#   (PR #25 second-pass auditor MEDIUM).
# - ``missing``: KS#3 (negative-keyword floor) operator-supplied
#   negative keyword phrases that a campaign lacks. Operators may
#   configure brand / competitor / sensitive terms in this list
#   (auditor M-2). NB: KS#3 also embeds the full list inside its
#   ``CheckResult.reason`` string via f-string interpolation — the
#   redactor cannot strip it from a free-form string. Tracked in
#   docs/BACKLOG.md as a follow-up against safety.py.
_PRIVATE_KEYS: frozenset[str] = frozenset({"new_queries_sample", "missing"})


def redact_for_audit(value: Any) -> Any:
    """Return ``value`` with privacy-sensitive keys removed, recursively.

    Walks dicts and lists. Leaves scalars alone. The redactor is
    deliberately a pure function so it can be tested in isolation
    and applied at multiple boundaries (sink-level here; potentially
    also at the service-layer if a future field needs it).
    """
    if isinstance(value, dict):
        return {k: redact_for_audit(v) for k, v in value.items() if k not in _PRIVATE_KEYS}
    if isinstance(value, list):
        return [redact_for_audit(item) for item in value]
    return value


class JsonlSink:
    """Append-only JSONL audit sink.

    One JSON object per line, written via ``asyncio.to_thread`` so the
    blocking ``open(..., "a")`` does not stall the event loop. The
    parent directory is created on first write — fresh deployments do
    not need to pre-create ``./logs/``.

    Privacy redaction is applied to every event's ``args`` / ``result``
    before the line is written; see :func:`redact_for_audit`.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    async def emit(self, event: AuditEvent) -> None:
        # Redact at the sink boundary — even if upstream emitted the
        # raw event, the on-disk file stays clean. We redact a copy of
        # the model's dict form rather than mutating the frozen event.
        data = event.model_dump(mode="json")
        data["args"] = redact_for_audit(data.get("args") or {})
        if data.get("result") is not None:
            data["result"] = redact_for_audit(data["result"])
        line = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        await asyncio.to_thread(self._append, line)

    def _append(self, line: str) -> None:
        # Synchronous helper run inside ``asyncio.to_thread``. Creates
        # the parent dir on demand; opens in append mode so concurrent
        # ``emit`` calls from the same process append cleanly. Cross-
        # process safety is out of scope (single-operator JSONL design,
        # see docs/BACKLOG.md "apply-plan concurrency / file-lock").
        #
        # Durability note (auditor M-3): ``open(...).close()`` flushes
        # the Python-level buffer to the OS, but does NOT call
        # ``fsync``. A power loss / SIGKILL between close-return and
        # the OS buffer flush silently loses the most recent event. For
        # a single-operator local audit log this is acceptable; if the
        # log ever needs durability guarantees (compliance, regulatory
        # archival) the fix is two lines: ``f.flush(); os.fsync(f.fileno())``
        # before the context manager exits, accepting the latency hit.
        # Tracked in docs/BACKLOG.md as a follow-up.
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")


# --------------------------------------------------------------------------
# audit_action — the only thing service methods need to import.
# --------------------------------------------------------------------------


@dataclass
class _AuditCtx:
    """Mutable scratchpad for the in-flight operation.

    Callers populate ``result`` / ``units_spent`` during the wrapped
    block; the context manager reads them at exit time.
    """

    _result: dict[str, Any] | None = field(default=None)
    _units_spent: int | None = field(default=None)

    def set_result(self, result: dict[str, Any]) -> None:
        self._result = result

    def set_units_spent(self, units: int) -> None:
        self._units_spent = units


@asynccontextmanager
async def audit_action(
    sink: AuditSink,
    *,
    actor: Actor,
    action: str,
    resource: str | None = None,
    args: dict[str, Any] | None = None,
    trace_id: str | None = None,
) -> AsyncIterator[_AuditCtx]:
    """Wrap an async operation in a paired ``.requested`` / ``.ok|.failed``
    audit emission.

    Usage::

        async with audit_action(sink, actor="agent", action="set_campaign_budget",
                                 args={"campaign_id": 42, "budget_rub": 800},
                                 resource="campaign:42",
                                 trace_id=trace_id) as ctx:
            result = await direct_api.update_campaign_budget(42, 800)
            ctx.set_result({"status": "applied", **result})

    Two events fire:

    1. ``set_campaign_budget.requested`` — emitted on context entry,
       carries ``args`` and ``resource``. ``result`` is ``None``.
    2. ``set_campaign_budget.ok`` — emitted on clean exit. Carries the
       caller's ``ctx.set_result(...)`` payload + ``units_spent``.

    On exception:

    2'. ``set_campaign_budget.failed`` — emitted before the exception
        propagates. ``result`` is the caller's set_result payload (or
        ``{}``) augmented with ``error_type`` and ``error_message``.
        The exception is NOT swallowed — re-raised after emission.

    Sink-level redaction strips privacy-sensitive keys from both
    ``args`` and ``result`` before persistence; see
    :func:`redact_for_audit`.
    """

    ctx = _AuditCtx()

    requested = AuditEvent(
        ts=datetime.now(UTC),
        actor=actor,
        action=f"{action}.requested",
        trace_id=trace_id,
        resource=resource,
        args=dict(args or {}),
    )
    # ``.requested`` emit happens BEFORE the wrapped block runs. If it
    # raises, the wrapped block never executes and the exception
    # propagates as-is — that's acceptable: no money was spent because
    # nothing happened yet, and the operator gets the I/O error directly.
    await sink.emit(requested)

    try:
        yield ctx
    except Exception as exc:
        # Build .failed event preserving any partial result the caller
        # already populated, then surface exception metadata. The audit
        # emit MUST NOT mask the original business-logic exception:
        # if disk-full happens here, the operator needs the underlying
        # API failure (or whatever raised inside the ``with`` block),
        # not the I/O error from the audit sink. Auditor C-1.
        result_payload: dict[str, Any] = dict(ctx._result or {})
        result_payload["error_type"] = type(exc).__name__
        result_payload["error_message"] = str(exc)
        failed = AuditEvent(
            ts=datetime.now(UTC),
            actor=actor,
            action=f"{action}.failed",
            trace_id=trace_id,
            resource=resource,
            args=dict(args or {}),
            result=result_payload,
        )
        try:
            await sink.emit(failed)
        except Exception:
            # Audit-write failure on the failure path — log as warning
            # but do NOT propagate; the original exception is the one
            # the operator must see. The .failed record is lost, but
            # the .requested record is on disk so the gap is visible
            # ("requested with no terminal event").
            structlog.get_logger(__name__).warning(
                "audit_emit_failed_in_failure_path",
                action=f"{action}.failed",
                error_type=type(exc).__name__,
            )
        raise

    ok = AuditEvent(
        ts=datetime.now(UTC),
        actor=actor,
        action=f"{action}.ok",
        trace_id=trace_id,
        resource=resource,
        args=dict(args or {}),
        result=ctx._result,
        units_spent=ctx._units_spent,
    )
    try:
        await sink.emit(ok)
    except Exception:
        # Audit-write failure on the success path — the wrapped
        # operation already succeeded, so we MUST NOT raise an
        # exception here that makes the caller think the API call
        # failed. Same auditor C-1 reasoning: emit failures lose
        # evidence but never mask outcome. Loss is visible from
        # the JSONL gap (requested with no terminal event).
        structlog.get_logger(__name__).warning(
            "audit_emit_failed_in_success_path",
            action=f"{action}.ok",
        )
