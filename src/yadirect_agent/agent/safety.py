"""Safety layer — kill-switches, policy, plan→confirm→execute.

This PR (M2 kill-switch #1) implements only the budget-cap slice:

- ``BudgetCapPolicy`` — the subset of the full policy schema relevant to
  kill-switch #1 (see ``docs/TECHNICAL_SPEC.md`` §M2.1 for the full schema
  that lands in M2.1).
- ``AccountBudgetSnapshot`` and ``BudgetChange`` — the data shapes the
  check operates on. Snapshot = what Direct says about the account right
  now; changes = what the agent wants to do.
- ``BudgetCapCheck`` — projects the changes onto the snapshot and blocks
  if any cap is breached.
- ``CheckResult`` — the canonical result shape that every future
  kill-switch will return.

Later milestones extend this file:
- M2.0 #2-#7: six more ``*Check`` classes alongside ``BudgetCapCheck``.
- M2.1: full ``Policy`` model (currently a narrow slice).
- M2.2: ``OperationPlan`` + ``@requires_plan`` decorator + pipeline that
  runs every check in sequence and blocks on the first failure.
- M2.3: audit sink wired into each check's invocation.

Skeleton commit — ``BudgetCapCheck.check`` is a stub that always returns
``blocked`` so tests fail on the specific wrong answer (right reason)
rather than ``ImportError`` (wrong reason).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

CheckStatus = Literal["ok", "blocked", "warn"]


# --------------------------------------------------------------------------
# CheckResult — shared by every kill-switch.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CheckResult:
    """Outcome of a single safety check.

    - ``ok``: operation may proceed.
    - ``blocked``: operation must not proceed. ``reason`` is surfaced to
      the human / agent as a user-visible message.
    - ``warn``: operation may proceed but something looks odd (e.g.
      approaching a cap); logged but does not stop the pipeline.
    """

    status: CheckStatus
    reason: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def ok_result(cls, **details: Any) -> CheckResult:
        return cls(status="ok", reason=None, details=details)

    @classmethod
    def blocked_result(cls, reason: str, **details: Any) -> CheckResult:
        return cls(status="blocked", reason=reason, details=details)

    @classmethod
    def warn_result(cls, reason: str, **details: Any) -> CheckResult:
        return cls(status="warn", reason=reason, details=details)


# --------------------------------------------------------------------------
# Account-level data shapes.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CampaignBudget:
    """Snapshot of one campaign's budget-relevant state."""

    id: int
    name: str
    daily_budget_rub: float
    state: str  # "ON" | "SUSPENDED" | "OFF" | "ENDED" | ...
    group: str | None = None  # None = no group label; unscoped by group caps


@dataclass(frozen=True)
class AccountBudgetSnapshot:
    """Current state of every campaign whose budget we care about."""

    campaigns: list[CampaignBudget] = field(default_factory=list)

    def total_active_budget_rub(self) -> float:
        """Sum of daily budgets of campaigns that are actually spending (state=ON)."""
        return sum(c.daily_budget_rub for c in self.campaigns if c.state == "ON")

    def group_active_budget_rub(self, group: str) -> float:
        """Sum of daily budgets of ON campaigns assigned to ``group``."""
        return sum(
            c.daily_budget_rub for c in self.campaigns if c.state == "ON" and c.group == group
        )


@dataclass(frozen=True)
class BudgetChange:
    """A proposed change to a single campaign's budget-relevant state.

    A field set to ``None`` means "leave that property as-is". This lets
    a single object describe a budget change, a resume/pause, or both.
    """

    campaign_id: int
    new_daily_budget_rub: float | None = None
    new_state: str | None = None


# --------------------------------------------------------------------------
# Policy schema (budget-cap slice only — full Policy in M2.1).
# --------------------------------------------------------------------------


class BudgetCapPolicy(BaseModel):
    """Kill-switch #1 policy slice.

    ``account_daily_budget_cap_rub`` is mandatory — the agent refuses to
    run without an explicit account ceiling. ``campaign_group_caps_rub``
    is optional; missing keys mean a group is unconstrained (bounded
    only by the account cap).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    account_daily_budget_cap_rub: int = Field(
        ...,
        ge=0,
        description="Hard ceiling on the sum of active campaign daily budgets.",
    )
    campaign_group_caps_rub: dict[str, int] = Field(
        default_factory=dict,
        description="Optional per-group ceilings (group name → RUB).",
    )


def load_budget_cap_policy(path: Path) -> BudgetCapPolicy:
    """Read ``agent_policy.yml`` and extract the budget-cap slice.

    Parses the full YAML but only validates the fields this PR cares
    about. Unknown top-level keys are tolerated — M2.1 will land the
    remaining fields without breaking existing files.
    """
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    budget_fields = {
        k: raw[k] for k in ("account_daily_budget_cap_rub", "campaign_group_caps_rub") if k in raw
    }
    return BudgetCapPolicy.model_validate(budget_fields)


# --------------------------------------------------------------------------
# BudgetCapCheck — kill-switch #1.
# --------------------------------------------------------------------------


class BudgetCapCheck:
    """Block plans that would push daily spend over a configured cap.

    Skeleton: always blocks. Real projection + comparison lands in the
    next commit.
    """

    def __init__(self, policy: BudgetCapPolicy) -> None:
        self._policy = policy

    def check(
        self,
        snapshot: AccountBudgetSnapshot,
        changes: list[BudgetChange],
    ) -> CheckResult:
        # Stub — overriden by the GREEN commit that implements the real
        # projection + cap comparison.
        _ = snapshot
        _ = changes
        return CheckResult.blocked_result("not implemented")
