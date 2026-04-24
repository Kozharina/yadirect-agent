"""Operation plans — the data layer of M2.2 plan → confirm → execute.

This module introduces *only* the data model and storage for proposed
mutating operations. The orchestrator that produces plans (via the
``@requires_plan`` decorator) and the executor that acts on approved
plans land in the next PR. Keeping the model + storage isolated lets
it be audited on its own.

Shape of the flow (partially, today):

1. Agent decides a write is needed.
2. M2.2 pipeline wraps the call, builds an ``OperationPlan`` from the
   call site + policy check results.
3. If the plan needs human confirmation, it's appended to
   ``pending_plans.jsonl``; the agent returns.
4. Operator runs ``yadirect-agent plans list`` / ``plans show <id>``
   to review.
5. (Next PR) Operator runs ``yadirect-agent apply-plan <id>`` →
   executor resolves the plan against services, emits audit events,
   updates the plan's status.

``pending_plans.jsonl`` is append-only: status changes append a new
line rather than rewrite. Readers collapse by plan_id and keep the
latest entry per id. This is deliberate — the file doubles as a
tamper-evident audit trail until the M2.3 audit sink lands with
stronger guarantees.
"""

from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

PlanStatus = Literal["pending", "approved", "rejected", "applied", "failed"]
#: ``failed`` marks a plan whose apply-plan executor raised during the
#: underlying API call. The plan cannot be retried through the normal
#: ``apply-plan`` path — operators must triage the failure reason and
#: decide whether to re-propose from scratch. Keeping it as a terminal
#: status (alongside ``rejected`` and ``applied``) preserves the
#: append-only audit trail and ensures a failed plan never silently
#: re-runs on a later snapshot where the decision might differ.


class OperationPlan(BaseModel):
    """A proposed mutating operation waiting for a decision.

    Every field is required except ``args`` / ``resource_ids`` /
    ``trace_id`` / ``review_context``. Frozen: the content of a plan
    never changes — state transitions produce *new* plan entries
    (with the same ``plan_id`` and a new ``status`` +
    ``status_updated_at``) that the store appends.

    ``preview`` is the one-line human-readable summary the CLI
    surfaces; ``reason`` is why the pipeline decided this plan
    required confirmation (e.g. "bid increase > 50% on 12 keywords").

    ``review_context`` carries a serialised snapshot of the
    ``ReviewContext`` that produced the pipeline's decision at plan
    creation time. It's consumed by the apply-plan executor to
    (a) re-review the plan against the same snapshot that made the
    confirm decision and (b) call ``SafetyPipeline.on_applied`` with
    the original context so the session TOCTOU register records the
    approval at the originally-evaluated ceiling.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    plan_id: str = Field(..., min_length=1)
    created_at: datetime
    action: str = Field(..., min_length=1)
    resource_type: str = Field(..., min_length=1)
    resource_ids: list[int] = Field(default_factory=list)
    args: dict[str, Any] = Field(default_factory=dict)
    preview: str = Field(..., min_length=1)
    reason: str = Field(..., min_length=1)
    status: PlanStatus = "pending"
    status_updated_at: datetime | None = None
    trace_id: str | None = None
    review_context: dict[str, Any] | None = None

    @field_validator("plan_id")
    @classmethod
    def _no_whitespace_in_plan_id(cls, v: str) -> str:
        if v != v.strip() or any(ch.isspace() for ch in v):
            msg = "plan_id must not contain whitespace"
            raise ValueError(msg)
        return v


def generate_plan_id() -> str:
    """Return a short, URL-safe, cryptographically-random plan id.

    Short enough for operators to paste on the command line, long
    enough to avoid collisions over the lifetime of one agent_policy
    deployment (16 hex chars = 64 bits of entropy).
    """
    return secrets.token_hex(8)


class PendingPlansStore:
    """Append-only JSONL store of OperationPlan entries.

    Every state change (create / approve / reject / apply) appends a
    new line; callers never mutate existing rows. Readers collapse by
    ``plan_id`` and keep the most-recent entry, so the store is
    effectively last-write-wins while the on-disk log is tamper-
    evident.

    The file is created on first write. Reads on a missing file
    return an empty list — a fresh deployment simply has no history.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    # -- Writes ----------------------------------------------------------

    def append(self, plan: OperationPlan) -> None:
        """Append ``plan`` as a JSON line. Creates parent dirs on demand."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as f:
            f.write(plan.model_dump_json() + "\n")

    def update_status(
        self,
        plan_id: str,
        new_status: PlanStatus,
    ) -> OperationPlan:
        """Append a new row with updated status for ``plan_id``.

        Raises KeyError if the plan is not found. Returns the new
        (updated) plan instance for the caller's convenience.
        """
        existing = self.get(plan_id)
        if existing is None:
            msg = f"plan not found: {plan_id!r}"
            raise KeyError(msg)
        updated = existing.model_copy(
            update={
                "status": new_status,
                "status_updated_at": datetime.now(UTC),
            }
        )
        self.append(updated)
        return updated

    # -- Reads -----------------------------------------------------------

    def all_plans(self) -> list[OperationPlan]:
        """Every plan with the latest status per plan_id, oldest first."""
        return list(self._collapse_by_id().values())

    def list_pending(self) -> list[OperationPlan]:
        """Plans whose current status is ``pending`` (i.e. waiting for a decision)."""
        return [p for p in self.all_plans() if p.status == "pending"]

    def get(self, plan_id: str) -> OperationPlan | None:
        """Return the latest state of ``plan_id``, or None if unknown."""
        return self._collapse_by_id().get(plan_id)

    # -- Internals -------------------------------------------------------

    def _collapse_by_id(self) -> dict[str, OperationPlan]:
        """Scan the file and keep the latest entry per plan_id."""
        if not self._path.exists():
            return {}
        out: dict[str, OperationPlan] = {}
        with self._path.open(encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    plan = OperationPlan.model_validate_json(line)
                except (json.JSONDecodeError, ValueError):
                    # A corrupt line doesn't invalidate the rest; skip.
                    # The M2.3 audit sink will catch this structurally.
                    continue
                out[plan.plan_id] = plan
        return out
