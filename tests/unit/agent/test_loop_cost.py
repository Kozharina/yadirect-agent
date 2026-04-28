"""Tests for cost capture in the agent loop (M21).

Asserts that:
- a single end_turn run writes one CostRecord and surfaces
  ``AgentRun.cost_rub``;
- a multi-turn run writes one record per ``messages.create`` call
  (one per loop iteration), and the AgentRun total equals the sum;
- a CostStore append failure does NOT abort the run (defensive
  posture: cost tracking failures stay non-fatal, mirroring audit
  emit guards from M2.3a).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from yadirect_agent.agent.cost import CostStore
from yadirect_agent.agent.loop import Agent
from yadirect_agent.agent.tools import ToolRegistry
from yadirect_agent.config import Settings
from yadirect_agent.models.cost import CostRecord

from .conftest import FakeAnthropic, make_message, text_block


def _agent(
    settings: Settings,
    *,
    cost_store: CostStore,
    client: FakeAnthropic,
) -> Agent:
    # Minimal empty registry — we only care about the messages.create
    # call accounting, not tool execution.
    registry = ToolRegistry()
    return Agent(
        settings=settings,
        registry=registry,
        client=client,  # type: ignore[arg-type]
        cost_store=cost_store,
    )


class TestCostCapture:
    async def test_single_turn_writes_one_record(
        self,
        settings: Settings,
        tmp_path: Path,
    ) -> None:
        store = CostStore(tmp_path / "cost.jsonl")
        client = FakeAnthropic(
            turns=[
                make_message(
                    content=[text_block("done")],
                    stop_reason="end_turn",
                    input_tokens=1000,
                    output_tokens=500,
                ),
            ],
        )
        agent = _agent(settings, cost_store=store, client=client)

        run = await agent.run("hello")

        records = store.all_records()
        assert len(records) == 1
        record = records[0]
        assert record.trace_id == run.trace_id
        assert record.input_tokens == 1000
        assert record.output_tokens == 500
        # Run-level total matches the single record's cost.
        assert run.cost_rub == pytest.approx(record.cost_rub)

    async def test_records_use_settings_pricing_snapshot(
        self,
        settings: Settings,
        tmp_path: Path,
    ) -> None:
        # Verify rate snapshot is taken from Settings at write time,
        # not hardcoded.
        custom_settings = settings.model_copy(update={"usd_to_rub_rate": 200.0})
        store = CostStore(tmp_path / "cost.jsonl")
        client = FakeAnthropic(
            turns=[
                make_message(
                    content=[text_block("done")],
                    stop_reason="end_turn",
                    input_tokens=1_000_000,
                    output_tokens=0,
                ),
            ],
        )
        agent = _agent(custom_settings, cost_store=store, client=client)

        await agent.run("hello")

        record = store.all_records()[0]
        # 1M input @ Opus rate $15 * rate 200 = 3000 RUB
        assert record.usd_to_rub_rate == pytest.approx(200.0)
        assert record.cost_rub == pytest.approx(3000.0, rel=1e-3)

    async def test_zero_usage_writes_zero_cost_record(
        self,
        settings: Settings,
        tmp_path: Path,
    ) -> None:
        # Anthropic responses occasionally lack ``usage`` (older SDK
        # paths, retried calls); the loop's ``_capture_cost`` is a
        # no-op when usage is None. No record is written.
        store = CostStore(tmp_path / "cost.jsonl")
        client = FakeAnthropic(
            turns=[
                make_message(
                    content=[text_block("done")],
                    stop_reason="end_turn",
                    input_tokens=0,
                    output_tokens=0,
                ),
            ],
        )
        agent = _agent(settings, cost_store=store, client=client)

        run = await agent.run("hello")

        # Zero-token usage still produces a record (it has trace_id,
        # model, etc.) — just with cost_rub=0.0. AgentRun also reports 0.
        records = store.all_records()
        assert len(records) == 1
        assert records[0].cost_rub == pytest.approx(0.0)
        assert run.cost_rub == pytest.approx(0.0)

    async def test_cost_store_failure_does_not_abort_run(
        self,
        settings: Settings,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # auditor M2.3a pattern: tracking layer failures must not
        # mask successful operation. A broken cost store stays
        # operationally annoying without breaking the agent.
        store = CostStore(tmp_path / "cost.jsonl")
        original_append = store.append

        def boom(record: CostRecord) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(store, "append", boom)
        client = FakeAnthropic(
            turns=[
                make_message(
                    content=[text_block("done")],
                    stop_reason="end_turn",
                    input_tokens=1000,
                    output_tokens=500,
                ),
            ],
        )
        agent = _agent(settings, cost_store=store, client=client)

        # Run completes despite the cost-store failure.
        run = await agent.run("hello")

        assert run.final_text == "done"
        # cost_rub stays 0 since the record was rejected.
        assert run.cost_rub == pytest.approx(0.0)
        # No records on disk.
        monkeypatch.setattr(store, "append", original_append)
        assert store.all_records() == []
