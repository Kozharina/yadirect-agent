"""Tests for ``BudgetGuard`` — M21.2 cost-budget enforcement.

Closes the auto-degrade loop: when the operator's monthly LLM spend
crosses ``Settings.agent_monthly_llm_budget_rub``, the agent loop
refuses to run further LLM calls AND fires a HIGH-severity
notification through ``NotificationDispatcher`` (built on M18
slice 5a). The two parts are deliberately fused into one component
because shipping enforcement without the alert path was the
original blocker — silent enforcement is a worse failure mode than
silent over-spend.

Contracts pinned here:

1. **Disabled by default.** ``budget_rub=None`` means the operator
   never set a budget; ``check_or_raise`` is a no-op so the agent
   loop runs unchanged for users not opting in.
2. **Soft cutoff.** Spend < budget → pass. Spend >= budget → raise.
   The LAST call before crossing finishes (cost is appended only
   AFTER the messages.create response), then the NEXT iteration's
   pre-check catches the now-crossed state. No mid-call interruption.
3. **One alert per process.** On first exhaustion, the guard
   dispatches a Notification. On every subsequent ``check_or_raise``
   in the same process, alert is suppressed but the exception still
   fires. Without the dedup, every retry in a tight loop would page
   the operator.
4. **Dispatcher-optional.** No dispatcher wired (or empty
   dispatcher) → enforcement still works, just without the alert.
   Caller still sees BudgetExhaustedError.
5. **Month-scoped.** Only the current calendar month counts;
   previous months' spend doesn't count against the current
   budget. Injected ``clock`` makes the month boundary testable.
6. **``from_settings`` ergonomics.** One-line assembly from a
   ``Settings`` + optional dispatcher — same shape the other
   services use.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import SecretStr

from yadirect_agent.agent.cost import CostStore
from yadirect_agent.models.cost import CostRecord
from yadirect_agent.models.health import Severity
from yadirect_agent.models.notification import Notification
from yadirect_agent.services.cost_budget import (
    BudgetExhaustedError,
    BudgetGuard,
)
from yadirect_agent.services.notify.dispatcher import NotificationDispatcher


def _make_record(*, cost_rub: float, when: datetime) -> CostRecord:
    """Build a CostRecord whose fields are valid pydantic but
    only ``cost_rub`` + ``timestamp`` matter for budget arithmetic.
    """
    return CostRecord(
        timestamp=when,
        trace_id="trace-test",
        model="claude-opus-4-7",
        input_tokens=100,
        output_tokens=200,
        cached_input_tokens=0,
        input_usd_per_million=15.0,
        output_usd_per_million=75.0,
        cached_input_usd_per_million=1.5,
        usd_to_rub_rate=100.0,
        cost_rub=cost_rub,
    )


@pytest.fixture
def cost_store(tmp_path: Path) -> CostStore:
    return CostStore(tmp_path / "cost.jsonl")


def _fixed_clock(year: int = 2026, month: int = 5, day: int = 22) -> datetime:
    """Deterministic month for the guard's "current month" check.

    May 2026 — same as CHANGELOG. The day component is irrelevant
    to month-bucketing arithmetic but keeps logs realistic.
    """
    return datetime(year, month, day, 12, 0, tzinfo=UTC)


class _RecordingSink:
    """Captures notifications sent through the dispatcher (no httpx)."""

    def __init__(self) -> None:
        self.received: list[Notification] = []

    async def send(self, notification: Notification) -> None:
        self.received.append(notification)


class TestEnforcementDisabled:
    @pytest.mark.asyncio
    async def test_check_is_noop_when_budget_none(self, cost_store: CostStore) -> None:
        # Backward-compat: pre-M21.2 users + new users who haven't
        # set agent_monthly_llm_budget_rub continue to run unbounded.
        # No exception, no alert, no even a cost-store read.
        guard = BudgetGuard(
            cost_store=cost_store,
            budget_rub=None,
            dispatcher=None,
            clock=_fixed_clock,
        )
        await guard.check_or_raise()  # must not raise

    def test_remaining_rub_returns_none_when_budget_disabled(self, cost_store: CostStore) -> None:
        guard = BudgetGuard(
            cost_store=cost_store,
            budget_rub=None,
            dispatcher=None,
            clock=_fixed_clock,
        )
        assert guard.remaining_rub() is None


class TestSoftCutoff:
    @pytest.mark.asyncio
    async def test_check_passes_when_under_budget(self, cost_store: CostStore) -> None:
        cost_store.append(_make_record(cost_rub=999.0, when=_fixed_clock()))
        guard = BudgetGuard(
            cost_store=cost_store,
            budget_rub=1_000.0,
            dispatcher=None,
            clock=_fixed_clock,
        )
        await guard.check_or_raise()  # 999 < 1000 — pass

    @pytest.mark.asyncio
    async def test_check_raises_when_at_budget(self, cost_store: CostStore) -> None:
        # Strict ``spent >= budget`` (not strictly-greater) — at the
        # threshold, refuse the next call. Operator who hit exactly
        # the budget meant "don't go over"; one-token-over is over.
        cost_store.append(_make_record(cost_rub=1_000.0, when=_fixed_clock()))
        guard = BudgetGuard(
            cost_store=cost_store,
            budget_rub=1_000.0,
            dispatcher=None,
            clock=_fixed_clock,
        )
        with pytest.raises(BudgetExhaustedError) as exc_info:
            await guard.check_or_raise()
        assert exc_info.value.spent_rub == pytest.approx(1_000.0)
        assert exc_info.value.budget_rub == pytest.approx(1_000.0)

    @pytest.mark.asyncio
    async def test_check_raises_when_over_budget(self, cost_store: CostStore) -> None:
        cost_store.append(_make_record(cost_rub=1_500.0, when=_fixed_clock()))
        guard = BudgetGuard(
            cost_store=cost_store,
            budget_rub=1_000.0,
            dispatcher=None,
            clock=_fixed_clock,
        )
        with pytest.raises(BudgetExhaustedError):
            await guard.check_or_raise()


class TestMonthScoping:
    @pytest.mark.asyncio
    async def test_only_current_month_counts(self, cost_store: CostStore) -> None:
        # Last month's spend doesn't count against this month's budget.
        # Without this, operators with a long history would hit
        # enforcement from day one of every new month.
        cost_store.append(_make_record(cost_rub=10_000.0, when=datetime(2026, 4, 30, tzinfo=UTC)))
        cost_store.append(_make_record(cost_rub=100.0, when=_fixed_clock()))  # May
        guard = BudgetGuard(
            cost_store=cost_store,
            budget_rub=1_000.0,
            dispatcher=None,
            clock=_fixed_clock,  # May 22
        )
        await guard.check_or_raise()  # only 100 < 1000 in May

    def test_remaining_rub_uses_only_current_month(self, cost_store: CostStore) -> None:
        cost_store.append(_make_record(cost_rub=10_000.0, when=datetime(2026, 4, 30, tzinfo=UTC)))
        cost_store.append(_make_record(cost_rub=250.0, when=_fixed_clock()))
        guard = BudgetGuard(
            cost_store=cost_store,
            budget_rub=1_000.0,
            dispatcher=None,
            clock=_fixed_clock,
        )
        assert guard.remaining_rub() == pytest.approx(750.0)


class TestAlertOnFirstExhaustion:
    @pytest.mark.asyncio
    async def test_alert_dispatched_on_first_exhaustion(self, cost_store: CostStore) -> None:
        sink = _RecordingSink()
        dispatcher = NotificationDispatcher(sinks=[sink])
        cost_store.append(_make_record(cost_rub=1_500.0, when=_fixed_clock()))
        guard = BudgetGuard(
            cost_store=cost_store,
            budget_rub=1_000.0,
            dispatcher=dispatcher,
            clock=_fixed_clock,
        )

        with pytest.raises(BudgetExhaustedError):
            await guard.check_or_raise()

        assert len(sink.received) == 1
        notification = sink.received[0]
        assert notification.severity == Severity.HIGH
        # Body must carry the actionable numbers so the operator
        # knows by how much they crossed without context-switching
        # to ``cost status``.
        assert "1500" in notification.body or "1 500" in notification.body
        assert "1000" in notification.body or "1 000" in notification.body

    @pytest.mark.asyncio
    async def test_alert_suppressed_on_subsequent_calls_same_process(
        self, cost_store: CostStore
    ) -> None:
        # In a tight retry loop, a 100-iter run would otherwise page
        # the operator 100 times. Dedup pins one alert per guard
        # lifetime; the second + onwards still raise (correct
        # behavior — caller must know it's exhausted) but stay silent.
        sink = _RecordingSink()
        dispatcher = NotificationDispatcher(sinks=[sink])
        cost_store.append(_make_record(cost_rub=1_500.0, when=_fixed_clock()))
        guard = BudgetGuard(
            cost_store=cost_store,
            budget_rub=1_000.0,
            dispatcher=dispatcher,
            clock=_fixed_clock,
        )

        for _ in range(3):
            with pytest.raises(BudgetExhaustedError):
                await guard.check_or_raise()

        # Exactly ONE notification across three exhaustion checks.
        assert len(sink.received) == 1

    @pytest.mark.asyncio
    async def test_no_alert_when_dispatcher_none(self, cost_store: CostStore) -> None:
        # Backward-compat / fresh install: no Telegram envs → no
        # dispatcher → enforcement still works (caller sees the
        # exception) but no crash trying to deliver.
        cost_store.append(_make_record(cost_rub=1_500.0, when=_fixed_clock()))
        guard = BudgetGuard(
            cost_store=cost_store,
            budget_rub=1_000.0,
            dispatcher=None,
            clock=_fixed_clock,
        )
        with pytest.raises(BudgetExhaustedError):
            await guard.check_or_raise()

    @pytest.mark.asyncio
    async def test_no_alert_when_dispatcher_disabled(self, cost_store: CostStore) -> None:
        # Empty Dispatcher (no sinks wired) — same shape as the no-
        # dispatcher case but exercises the is_enabled short-circuit
        # so we don't pay for a no-op send.
        empty_dispatcher = NotificationDispatcher(sinks=[])
        cost_store.append(_make_record(cost_rub=1_500.0, when=_fixed_clock()))
        guard = BudgetGuard(
            cost_store=cost_store,
            budget_rub=1_000.0,
            dispatcher=empty_dispatcher,
            clock=_fixed_clock,
        )
        with pytest.raises(BudgetExhaustedError):
            await guard.check_or_raise()
        # No sink would have received anything anyway, but pin the
        # contract: empty Dispatcher → no attempt to send.


class TestExhaustionError:
    def test_exception_carries_numbers_for_caller(self) -> None:
        exc = BudgetExhaustedError(spent_rub=1_500.0, budget_rub=1_000.0)
        assert exc.spent_rub == 1_500.0
        assert exc.budget_rub == 1_000.0
        # str() carries both numbers so logs / CLI render layer can
        # use it directly without re-formatting.
        msg = str(exc)
        assert "1500" in msg
        assert "1000" in msg


class TestFromSettings:
    def test_from_settings_picks_up_budget_and_assembles_store(self, tmp_path: Path) -> None:
        from yadirect_agent.config import Settings

        settings = Settings(
            yandex_direct_token=SecretStr("x"),
            yandex_metrika_token=SecretStr("x"),
            audit_log_path=tmp_path / "audit.jsonl",
            agent_policy_path=tmp_path / "policy.yml",
            agent_max_daily_budget_rub=10_000,
            agent_monthly_llm_budget_rub=500.0,
        )
        guard = BudgetGuard.from_settings(settings, dispatcher=None)
        # Budget pulled from Settings.
        assert guard.budget_rub == pytest.approx(500.0)
        # Cost store path sibling to audit log (matches CostStore
        # convention used in agent/loop.py default).
        assert guard.cost_store.path == tmp_path / "cost.jsonl"

    def test_from_settings_returns_disabled_guard_when_budget_none(self, tmp_path: Path) -> None:
        # Operator hasn't opted into enforcement. Guard exists for
        # caller-side ergonomics (avoid None-check at every call
        # site) but check_or_raise is a no-op.
        from yadirect_agent.config import Settings

        settings = Settings(
            yandex_direct_token=SecretStr("x"),
            yandex_metrika_token=SecretStr("x"),
            audit_log_path=tmp_path / "audit.jsonl",
            agent_policy_path=tmp_path / "policy.yml",
            agent_max_daily_budget_rub=10_000,
            agent_monthly_llm_budget_rub=None,
        )
        guard = BudgetGuard.from_settings(settings, dispatcher=None)
        assert guard.budget_rub is None
