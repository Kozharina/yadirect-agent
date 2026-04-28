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

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


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
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1)
    value: Any
    source: str = Field(..., min_length=1)
    """Where the value came from: ``"metrika"``, ``"direct"``,
    ``"snapshot"``, ``"policy"``. Free-form but should be a stable
    short identifier — used by future reporting (M12) to attribute
    findings."""

    observed_at: datetime
    """Wall-clock time the value was observed. Note: NOT the time the
    rationale is being constructed — those can differ by minutes when
    the agent loop builds context once and acts on it later."""


class Alternative(BaseModel):
    """One option the agent considered and rejected.

    Recording rejected alternatives is what separates "the agent has
    a reason" from "the agent has reasoning". An operator reading
    rationale should see the alternative and the rejection cause and
    be able to disagree on the spot (or accept).
    """

    model_config = ConfigDict(extra="forbid")

    description: str = Field(..., min_length=1)
    rejected_because: str = Field(..., min_length=1)


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

    model_config = ConfigDict(extra="forbid")

    decision_id: str = Field(..., min_length=1)
    """Aligned with ``OperationPlan.plan_id`` — same identifier for the
    same decision. Validators enforce no-whitespace consistent with
    ``OperationPlan.plan_id``."""

    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    """When the rationale was recorded. NOT when the inputs were
    observed — those carry their own timestamps in ``InputDataPoint``."""

    action: str = Field(..., min_length=1)
    """Mirrors ``OperationPlan.action`` — e.g.
    ``"campaigns.set_daily_budget"``. Lookup index for read-back like
    ``rationale list --action=set_daily_budget``."""

    resource_type: str = Field(..., min_length=1)
    resource_ids: list[int] = Field(default_factory=list)

    summary: str = Field(..., min_length=1, max_length=500)
    """One-to-two sentences for UI rendering. The Telegram approval card,
    the CLI table, and the future weekly digest all consume this. 500
    char cap is generous: longer than that and we have a "log entry",
    not a summary."""

    inputs: list[InputDataPoint] = Field(default_factory=list)
    """Data points the agent used. Empty list is allowed (some decisions
    are policy-only — "rejected because forbidden by agent_policy.yml")
    but most should have at least one."""

    alternatives_considered: list[Alternative] = Field(default_factory=list)
    """Options the agent rejected. Empty when the decision was
    obvious / one-of-a-kind. Populated for non-trivial decisions
    where multiple paths were live."""

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
