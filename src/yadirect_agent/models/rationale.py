"""Rationale model — human-readable explanation for an agent decision (M20).

Every mutating operation produces a ``Rationale`` that travels alongside
the ``OperationPlan`` through the safety pipeline. The contract:

- One ``Rationale`` per decision (``decision_id`` aligned with
  ``OperationPlan.plan_id`` so a plan and its reasoning share one
  identifier).
- Carries the *why*, not the *what* — what is captured by ``OperationPlan``
  (action, args, preview, resource_ids).
- Read-back is what makes shadow-week calibration possible: the operator
  asks "why did you do X?" and the agent retrieves the recorded rationale,
  not a fresh confabulation against a stale context.

Why a separate model from ``OperationPlan``:

- Plans are operational records: who proposed what, what the policy
  decided, did it apply. Their shape is stable across the safety
  surface.
- Rationale is interpretive: the agent's reasoning, the alternatives it
  considered, the data it used. Mixing the two would muddle two
  evolution rates — when M11 adds bid-strategy reasoning fields, those
  belong in ``Rationale``, not ``OperationPlan``.

Storage is a sibling JSONL next to ``audit.jsonl`` (``rationale.jsonl``).
Append-only, indexed by ``decision_id``. See ``agent/rationale_store.py``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Caps for free-text fields. Audit-relevant records flow into JSONL
# storage and (in future slices) into LLM context — uncapped strings
# create both storage-exhaustion and context-budget hazards.
# Same reasoning as M6 MEDIUM-3 (Metrika error message cap).
_MAX_SUMMARY_LEN = 500
_MAX_NAME_LEN = 100
_MAX_DESCRIPTION_LEN = 1000
_MAX_LIST_ITEMS = 50


class Confidence(StrEnum):
    """How sure the agent was when it made the decision.

    StrEnum so log/audit aggregations counting ``confidence:low`` events
    stay stable across rule additions. Three buckets is enough for
    shadow-week calibration:

    - ``low`` — the inputs were sparse or contradictory, the agent
      proceeded with significant uncertainty. Operator should
      double-check.
    - ``medium`` — typical case, enough data, no warning signals.
    - ``high`` — strong signal, multiple corroborating inputs, the
      agent would have made this call without hesitation.
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class InputDataPoint(BaseModel):
    """One data point the agent used to reach its decision.

    Captured at the moment of decision so a future read-back shows
    "we used CTR=4.2% as of 2026-04-20", not "let me re-fetch CTR now".
    Without timestamps, rationale becomes "the agent thinks X is good
    today" rather than "the agent thought X was good when it
    decided" — different question, different answer.

    Forward-compat: ``extra="ignore"`` so a future agent version
    adding a field doesn't cause silent record loss when an older
    binary reads the JSONL archive. Trade-off accepted because the
    JSONL is effectively a multi-version archive (read months after
    write); ``extra="forbid"`` would silently drop forward-format
    lines on rolling upgrades. (auditor M20 LOW-5.)
    """

    model_config = ConfigDict(extra="ignore")

    name: str = Field(..., min_length=1, max_length=_MAX_NAME_LEN)
    value: Any
    source: str = Field(..., min_length=1, max_length=_MAX_NAME_LEN)
    """Where the value came from: ``"metrika"``, ``"direct"``,
    ``"snapshot"``, ``"policy"``. Free-form but should be a stable
    short identifier — used by future reporting (M12) to attribute
    findings."""

    observed_at: datetime
    """Wall-clock time the value was observed. Note: NOT the time the
    rationale is being constructed — those can differ by minutes when
    the agent loop builds context once and acts on it later."""

    @field_validator("value")
    @classmethod
    def _value_must_be_json_serialisable(cls, v: Any) -> Any:
        """Reject non-JSON values at construction, not at JSONL write time.

        Without this, a caller passing ``datetime``, ``Decimal``,
        ``set``, a custom class, or ``float('nan')``/``float('inf')``
        would only see the failure deep inside ``RationaleStore.append``,
        which propagates out of ``_emit_rationale`` and aborts the
        ``@requires_plan`` flow entirely. Validating here surfaces the
        bug to the caller at the obvious point. (auditor M20 LOW-3.)
        """
        try:
            json.dumps(v, allow_nan=False)
        except (TypeError, ValueError) as exc:
            msg = f"InputDataPoint.value must be JSON-serialisable (no NaN/Inf): {exc}"
            raise ValueError(msg) from exc
        return v


class Alternative(BaseModel):
    """One option the agent considered and rejected.

    Recording rejected alternatives is what separates "the agent has
    a reason" from "the agent has reasoning". An operator reading
    rationale should see the alternative and the rejection cause and
    be able to disagree on the spot (or accept).
    """

    model_config = ConfigDict(extra="ignore")

    description: str = Field(..., min_length=1, max_length=_MAX_DESCRIPTION_LEN)
    rejected_because: str = Field(..., min_length=1, max_length=_MAX_DESCRIPTION_LEN)


class Rationale(BaseModel):
    """Human-readable explanation for one agent decision.

    Pinned by ``decision_id`` to an ``OperationPlan.plan_id`` (1:1).
    The pipeline emits this BEFORE persisting the plan; the ``apply_plan``
    re-entry path does NOT re-emit (rationale is a property of the
    decision, not of execution).

    Backward compat: until all callers update, ``rationale`` can be
    omitted from ``@requires_plan`` calls and a structlog warning will
    fire instead. A subsequent slice flips this to hard-required.
    """

    model_config = ConfigDict(extra="ignore")

    decision_id: str = Field(..., min_length=1)
    """Aligned with ``OperationPlan.plan_id`` — same identifier for the
    same decision. Validator enforces no-whitespace consistent with
    ``OperationPlan.plan_id`` (auditor M20 MEDIUM-2)."""

    @field_validator("decision_id")
    @classmethod
    def _no_whitespace_in_decision_id(cls, v: str) -> str:
        if any(ch.isspace() for ch in v):
            msg = "decision_id must not contain whitespace"
            raise ValueError(msg)
        return v

    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    """When the rationale was recorded. NOT when the inputs were
    observed — those carry their own timestamps in ``InputDataPoint``."""

    action: str = Field(..., min_length=1)
    """Mirrors ``OperationPlan.action`` — e.g.
    ``"campaigns.set_daily_budget"``. Lookup index for read-back like
    ``rationale list --action=set_daily_budget``."""

    resource_type: str = Field(..., min_length=1)
    resource_ids: list[int] = Field(default_factory=list)

    summary: str = Field(..., min_length=1, max_length=_MAX_SUMMARY_LEN)
    """One-to-two sentences for UI rendering. The Telegram approval card,
    the CLI table, and the future weekly digest all consume this. 500
    char cap is generous: longer than that and we have a "log entry",
    not a summary."""

    inputs: list[InputDataPoint] = Field(
        default_factory=list,
        max_length=_MAX_LIST_ITEMS,
    )
    """Data points the agent used. Empty list is allowed (some decisions
    are policy-only — "rejected because forbidden by agent_policy.yml")
    but most should have at least one. Capped at 50 items: a decision
    needing more than 50 input points is almost certainly bug-shaped
    rather than legitimately complex. (auditor M20 LOW-4.)"""

    alternatives_considered: list[Alternative] = Field(
        default_factory=list,
        max_length=_MAX_LIST_ITEMS,
    )
    """Options the agent rejected. Empty when the decision was
    obvious / one-of-a-kind. Populated for non-trivial decisions
    where multiple paths were live. Same 50-item cap as ``inputs``."""

    policy_slack: dict[str, float] = Field(default_factory=dict)
    """Distance to each kill-switch threshold the decision touched, as
    {check_name: slack_value}. ``"max_cpc": 12.5`` means we were 12.5
    RUB below the campaign max-CPC ceiling. Keys are stable check
    identifiers from the safety pipeline. Auto-populated in a future
    slice — for now the caller can fill it in manually."""

    confidence: Confidence = Confidence.MEDIUM
    """How sure the agent was. Defaults to ``medium`` so a caller
    that doesn't think about confidence doesn't accidentally claim
    ``high``."""
