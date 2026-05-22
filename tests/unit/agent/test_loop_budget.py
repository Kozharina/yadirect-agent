"""Tests for ``Agent`` ↔ ``BudgetGuard`` integration (M21.2).

The budget-guard enforcement seam: when the operator has set
``Settings.agent_monthly_llm_budget_rub`` and month-to-date spend
has crossed it, the agent loop must refuse to make further
``messages.create`` calls and let ``BudgetExhaustedError``
propagate to the caller.

Contracts pinned here:

1. **No guard, no enforcement.** Existing constructions (positional
   args, no ``budget_guard`` kwarg) keep working — backward compat.
2. **Pre-call check.** The guard runs BEFORE each ``messages.create``,
   not after. If exhausted, we never spend the call that would push
   us further over.
3. **Last-call-finishes semantics.** A run that starts under budget
   but crosses on iteration N — iteration N's response is captured
   (cost appended); iteration N+1's pre-check raises and aborts.
4. **Exception escapes the loop.** ``BudgetExhaustedError`` propagates
   to ``Agent.run``'s caller (not swallowed, not converted to an
   AgentRun with a special stop_reason). CLI / MCP entry points
   translate it to their respective render layers.
5. **Cost capture for completed iterations is preserved.** Even when
   the run aborts mid-way, the records for completed iterations
   stay in the CostStore so ``cost status`` shows the actual spend.

Coverage focuses on the wiring contract — BudgetGuard's own
behavior is exhaustively tested in
``tests/unit/services/test_cost_budget.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from yadirect_agent.agent.cost import CostStore
from yadirect_agent.agent.loop import Agent
from yadirect_agent.agent.tools import ToolRegistry
from yadirect_agent.config import Settings
from yadirect_agent.models.cost import CostRecord
from yadirect_agent.services.cost_budget import BudgetExhaustedError, BudgetGuard

from .conftest import FakeAnthropic, make_message, text_block


def _agent(
    settings: Settings,
    *,
    cost_store: CostStore,
    client: FakeAnthropic,
    budget_guard: BudgetGuard | None = None,
) -> Agent:
    registry = ToolRegistry()
    return Agent(
        settings=settings,
        registry=registry,
        client=client,  # type: ignore[arg-type]
        cost_store=cost_store,
        budget_guard=budget_guard,
    )


def _seed_cost_store(store: CostStore, *, cost_rub: float) -> None:
    """Append a prior CostRecord so the budget guard sees pre-existing spend."""
    store.append(
        CostRecord(
            timestamp=datetime(2026, 5, 22, 10, 0, tzinfo=UTC),
            trace_id="prior-trace",
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
    )


def _may_clock() -> datetime:
    return datetime(2026, 5, 22, 12, 0, tzinfo=UTC)


class TestBackwardCompat:
    async def test_no_budget_guard_passed_runs_normally(
        self,
        settings: Settings,
        tmp_path: Path,
    ) -> None:
        # Pre-M21.2 call shape — no budget_guard kwarg. Agent constructs,
        # runs, returns normally. Without this guard, the M21.2 wiring
        # would break every existing caller (CLI run, MCP tool calls,
        # acceptance tests).
        store = CostStore(tmp_path / "cost.jsonl")
        client = FakeAnthropic(
            turns=[
                make_message(
                    content=[text_block("ok")],
                    stop_reason="end_turn",
                    input_tokens=1,
                    output_tokens=1,
                ),
            ],
        )
        agent = _agent(settings, cost_store=store, client=client)  # no guard

        run = await agent.run("hello")

        assert run.stop_reason == "end_turn"
        assert len(client.calls) == 1


class TestPreCallEnforcement:
    async def test_run_raises_immediately_when_already_exhausted(
        self,
        settings: Settings,
        tmp_path: Path,
    ) -> None:
        # Operator started the agent on a month where they've already
        # spent past budget (e.g. caught up from a backfill or a
        # different process). FIRST iteration's pre-check must raise
        # — no LLM call gets made.
        store = CostStore(tmp_path / "cost.jsonl")
        _seed_cost_store(store, cost_rub=1_500.0)

        guard = BudgetGuard(
            cost_store=store,
            budget_rub=1_000.0,
            dispatcher=None,
            clock=_may_clock,
        )

        # No turns scripted — FakeAnthropic would raise AssertionError
        # if .create() were called. That's how we verify the loop
        # never made the LLM call.
        client = FakeAnthropic(turns=[])
        agent = _agent(settings, cost_store=store, client=client, budget_guard=guard)

        with pytest.raises(BudgetExhaustedError) as exc_info:
            await agent.run("hello")
        assert exc_info.value.spent_rub == pytest.approx(1_500.0)
        assert exc_info.value.budget_rub == pytest.approx(1_000.0)
        assert len(client.calls) == 0

    async def test_run_completes_when_under_budget(
        self,
        settings: Settings,
        tmp_path: Path,
    ) -> None:
        # Sanity: with guard wired but well under budget, the run
        # completes normally — guard is invisible to the happy path.
        store = CostStore(tmp_path / "cost.jsonl")
        _seed_cost_store(store, cost_rub=100.0)

        guard = BudgetGuard(
            cost_store=store,
            budget_rub=1_000.0,
            dispatcher=None,
            clock=_may_clock,
        )

        client = FakeAnthropic(
            turns=[
                make_message(
                    content=[text_block("done")],
                    stop_reason="end_turn",
                    input_tokens=1,
                    output_tokens=1,
                ),
            ],
        )
        agent = _agent(settings, cost_store=store, client=client, budget_guard=guard)

        run = await agent.run("hello")
        assert run.stop_reason == "end_turn"


class TestMidRunExhaustion:
    async def test_aborts_mid_run_when_budget_crossed(
        self,
        settings: Settings,
        tmp_path: Path,
    ) -> None:
        # Started under budget; iteration 1's response pushes us
        # over (cost appended after the response). Iteration 2's
        # pre-check catches it and raises. Iteration 1's CostRecord
        # MUST stay in the store (it's real spend; abandoning it
        # would defeat ``cost status`` accounting).
        store = CostStore(tmp_path / "cost.jsonl")
        # Pre-existing spend: 900. Budget: 1000. Iteration 1 will
        # add ~150 RUB worth (15 USD/M * 100k input + 75 USD/M * 50k
        # output = $1.5 + $3.75 = $5.25 * 100 RUB/USD = 525 RUB).
        # Even just 100 input + 50 output is small but enough to
        # cross when seeded close enough. Use larger token counts
        # to make the crossing predictable.
        _seed_cost_store(store, cost_rub=900.0)

        guard = BudgetGuard(
            cost_store=store,
            budget_rub=1_000.0,
            dispatcher=None,
            clock=_may_clock,
        )

        # Iteration 1: tool_use (not end_turn) — so the loop wants
        # to continue. Iteration 2's pre-check should refuse.
        client = FakeAnthropic(
            turns=[
                make_message(
                    content=[text_block("thinking")],
                    stop_reason="tool_use",
                    input_tokens=100_000,
                    output_tokens=50_000,
                ),
                # Second turn never gets called because pre-check
                # raises BudgetExhaustedError first.
            ],
        )
        agent = _agent(settings, cost_store=store, client=client, budget_guard=guard)

        with pytest.raises(BudgetExhaustedError):
            await agent.run("hello")

        # Exactly one LLM call made (iteration 1 completed).
        assert len(client.calls) == 1
        # Iteration 1's CostRecord persisted (the cost is real
        # regardless of whether the run finished cleanly).
        all_records = store.all_records()
        # Seed record + iteration 1's record = 2.
        assert len(all_records) == 2
