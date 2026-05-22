"""Monthly LLM cost-budget enforcement + alert dispatch (M21.2).

Closes the auto-degrade loop opened by M21 (cost observability):
when the operator's month-to-date LLM spend crosses
``Settings.agent_monthly_llm_budget_rub``, the agent loop refuses
the next ``messages.create`` call AND fires a HIGH-severity
notification through ``NotificationDispatcher`` (built on M18
slice 5a).

Why fuse enforcement and alert into one component:

- Silent enforcement is a worse failure mode than silent
  over-spend. If the agent suddenly stops responding to operator
  requests with no explanation, the operator's recovery path is
  opaque ("why isn't it working?"). The alert tells them exactly
  what happened and what to fix (raise the budget, or wait for
  the next billing month).
- This was the documented blocker on M21.2 — the BACKLOG entry
  cited M18's alert path as the prerequisite. With Dispatcher
  shipped in slice 5a, this dependency clears.

Soft cutoff semantics (``spent >= budget`` raises, not ``>``):

- Last call BEFORE crossing finishes. CostStore.append happens
  AFTER the ``messages.create`` response, so the iteration that
  pushed spend across the threshold completes its work; the NEXT
  iteration's pre-check catches the now-crossed state. No mid-
  call interruption, no partially-consumed tokens charged but
  not used.
- ``>=`` rather than ``>``: operator who set 1000 RUB meant "don't
  go over"; hitting exactly 1000 should refuse the next call, not
  the one after.

One alert per process (dedup):

- Tight retry loops (an agent run with 20 iterations, all caught
  by the guard) would otherwise page the operator 20 times.
  ``_alerted_this_process`` pins exactly one notification per
  guard lifetime. Subsequent exhaustion checks still RAISE — the
  caller must know it's exhausted — but stay silent on the
  notification channel.

What this module does NOT do:

- Pro-rated daily / weekly budgets. The contract is a monthly
  ceiling; sub-month bucketing is a follow-up if operators ask
  for it.
- Multi-tenant per-account budgets. M14 (agency mode) territory.
- Auto-reset when a new month rolls over. Implicit via month
  scoping — the spend counter shifts to the new month
  automatically because the guard re-reads CostStore on each
  check_or_raise.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog

from ..agent.cost import CostStore
from ..models.health import Severity
from ..models.notification import Notification

if TYPE_CHECKING:
    from ..config import Settings
    from .notify.dispatcher import NotificationDispatcher

_log = structlog.get_logger(component="services.cost_budget")


class BudgetExhaustedError(Exception):
    """Raised by ``BudgetGuard.check_or_raise`` when month-to-date spend
    is at or above ``Settings.agent_monthly_llm_budget_rub``.

    Carries the numbers so callers (CLI, MCP, future scheduler) can
    format their own operator-facing message without re-querying the
    cost store. ``str()`` includes both values so the default
    rendering already has the actionable detail.
    """

    def __init__(self, *, spent_rub: float, budget_rub: float) -> None:
        self.spent_rub = spent_rub
        self.budget_rub = budget_rub
        super().__init__(
            f"LLM budget exhausted: {spent_rub:.0f}/{budget_rub:.0f} RUB month-to-date",
        )


class BudgetGuard:
    """Enforces ``agent_monthly_llm_budget_rub`` + dispatches alert on first exhaustion."""

    def __init__(
        self,
        *,
        cost_store: CostStore,
        budget_rub: float | None,
        dispatcher: NotificationDispatcher | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._cost_store = cost_store
        self._budget_rub = budget_rub
        self._dispatcher = dispatcher
        self._clock = clock
        # Process-level dedup flag. Reset only by constructing a new
        # guard instance — that's exactly the granularity we want
        # (one alert per agent run / per CLI invocation), since
        # callers construct one guard per top-level entry.
        self._alerted_this_process: bool = False

    # -- Public surface ----------------------------------------------------

    @property
    def cost_store(self) -> CostStore:
        """Exposed for ``from_settings`` test assertions only."""
        return self._cost_store

    @property
    def budget_rub(self) -> float | None:
        return self._budget_rub

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        dispatcher: NotificationDispatcher | None = None,
    ) -> BudgetGuard:
        """Assemble a guard from ``Settings`` + optional dispatcher.

        Cost store path is sibling to the audit log — matches the
        convention ``agent/loop.py`` uses for its default CostStore.
        Dispatcher is OPTIONAL: a caller that doesn't have one
        (tests, non-CLI entry points) gets a working guard that
        enforces but doesn't alert.
        """
        cost_store = CostStore(settings.audit_log_path.parent / "cost.jsonl")
        return cls(
            cost_store=cost_store,
            budget_rub=settings.agent_monthly_llm_budget_rub,
            dispatcher=dispatcher,
        )

    def remaining_rub(self) -> float | None:
        """How much budget is left this calendar month, or None if disabled."""
        if self._budget_rub is None:
            return None
        return self._budget_rub - self._current_month_spend_rub()

    async def check_or_raise(self) -> None:
        """Refuse the caller's next LLM call if month-to-date >= budget.

        No-op when ``budget_rub is None`` (enforcement disabled).
        On first exhaustion in this guard's lifetime, dispatches a
        HIGH-severity Notification through the configured
        Dispatcher (if any) BEFORE raising — operator gets the
        Telegram ping with the same numbers the exception carries.
        """
        if self._budget_rub is None:
            return  # enforcement disabled

        spend = self._current_month_spend_rub()
        if spend < self._budget_rub:
            return

        await self._alert_if_first_exhaustion(spend=spend)
        _log.warning(
            "cost_budget.exhausted",
            spent_rub=spend,
            budget_rub=self._budget_rub,
        )
        raise BudgetExhaustedError(spent_rub=spend, budget_rub=self._budget_rub)

    # -- Internals ---------------------------------------------------------

    def _current_month_spend_rub(self) -> float:
        """Sum of cost_rub across records in the current calendar month."""
        now = self._clock()
        records = self._cost_store.records_in_month(year=now.year, month=now.month)
        return sum(r.cost_rub for r in records)

    async def _alert_if_first_exhaustion(self, *, spend: float) -> None:
        """Dispatch a HIGH Notification through the Dispatcher, exactly once.

        Subsequent calls in the same process are silent (the
        exception still raises in ``check_or_raise``).
        """
        if self._alerted_this_process:
            return
        self._alerted_this_process = True

        if self._dispatcher is None or not self._dispatcher.is_enabled:
            return

        # Numbers without thousand separators so a grep-friendly
        # render across all locales. The CLI's render layer is
        # free to reformat for human reading; this body is the
        # canonical machine-and-human-readable representation.
        body = (
            f"Monthly LLM budget exhausted: {spend:.0f} / "
            f"{self._budget_rub:.0f} RUB month-to-date. "
            "The agent has been paused to prevent further spend. "
            "Raise agent_monthly_llm_budget_rub to continue, or "
            "wait until next month."
        )
        notification = Notification(
            severity=Severity.HIGH,
            title="yadirect-agent: LLM budget exhausted",
            body=body,
        )
        # Dispatcher swallows per-sink failures (M18 slice 5a
        # contract); a Telegram outage doesn't leak back into the
        # caller's BudgetExhaustedError handling.
        await self._dispatcher.send(notification)


__all__ = [
    "BudgetExhaustedError",
    "BudgetGuard",
]
