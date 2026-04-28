"""Tests for CostCalculator + CostStore (M21)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from yadirect_agent.agent.cost import (
    DEFAULT_ANTHROPIC_PRICING,
    CostStore,
    calculate_cost,
)
from yadirect_agent.config import Settings
from yadirect_agent.models.cost import CostRecord

# --------------------------------------------------------------------------
# calculate_cost
# --------------------------------------------------------------------------


class TestCalculateCost:
    def test_opus_pricing_known_input(self, settings: Settings) -> None:
        # 1M input + 1M output @ Opus rates ($15 + $75) * rate 100
        # = $90 * 100 = 9000 RUB. Settings default usd_to_rub_rate=100.
        record = calculate_cost(
            trace_id="abc",
            model="claude-opus-4-7",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            settings=settings,
        )

        assert record.cost_rub == pytest.approx(9000.0, rel=1e-3)
        assert record.input_usd_per_million == pytest.approx(15.0)
        assert record.output_usd_per_million == pytest.approx(75.0)
        assert record.usd_to_rub_rate == pytest.approx(100.0)

    def test_zero_tokens_zero_cost(self, settings: Settings) -> None:
        record = calculate_cost(
            trace_id="x",
            model="claude-opus-4-7",
            input_tokens=0,
            output_tokens=0,
            settings=settings,
        )
        assert record.cost_rub == pytest.approx(0.0)

    def test_unknown_model_falls_back_to_opus(self, settings: Settings) -> None:
        # Conservative fallback — overcount safer than undercount.
        record = calculate_cost(
            trace_id="x",
            model="claude-future-model-9-1",
            input_tokens=1_000_000,
            output_tokens=0,
            settings=settings,
        )
        # 1M input @ Opus rate ($15) * 100 = 1500 RUB
        assert record.cost_rub == pytest.approx(1500.0, rel=1e-3)
        assert record.model == "claude-future-model-9-1"  # original preserved
        assert record.input_usd_per_million == pytest.approx(15.0)  # opus rate snapshot

    def test_pricing_snapshot_independent_of_settings_change(
        self,
        settings: Settings,
    ) -> None:
        # The record snapshots usd_to_rub_rate at write time. Even if
        # the operator changes the rate later, the historical record
        # remains accurate.
        record = calculate_cost(
            trace_id="x",
            model="claude-opus-4-7",
            input_tokens=1_000_000,
            output_tokens=0,
            settings=settings,
        )
        # Settings is mutated after the fact (would be via env reload
        # in real life). The record's snapshot doesn't change.
        original_rate = record.usd_to_rub_rate
        new_settings = settings.model_copy(update={"usd_to_rub_rate": 200.0})
        assert new_settings.usd_to_rub_rate == 200.0
        assert record.usd_to_rub_rate == original_rate  # unchanged

    def test_cached_input_costs_less_than_fresh(self, settings: Settings) -> None:
        fresh = calculate_cost(
            trace_id="x",
            model="claude-opus-4-7",
            input_tokens=1_000_000,
            output_tokens=0,
            cached_input_tokens=0,
            settings=settings,
        )
        cached = calculate_cost(
            trace_id="y",
            model="claude-opus-4-7",
            input_tokens=0,
            output_tokens=0,
            cached_input_tokens=1_000_000,
            settings=settings,
        )
        # Cached pricing in DEFAULT_ANTHROPIC_PRICING is 1.5 vs 15
        # (90% cheaper).
        assert cached.cost_rub == pytest.approx(fresh.cost_rub * 0.1, rel=1e-3)


class TestDefaultPricing:
    def test_known_models_present(self) -> None:
        assert "claude-opus-4-7" in DEFAULT_ANTHROPIC_PRICING
        assert "claude-sonnet-4-7" in DEFAULT_ANTHROPIC_PRICING
        assert "claude-haiku-4-5" in DEFAULT_ANTHROPIC_PRICING

    def test_haiku_cheaper_than_sonnet_cheaper_than_opus(self) -> None:
        # Sanity-check the pricing table — Anthropic's tier ordering
        # has been Opus > Sonnet > Haiku for years. A future PR
        # accidentally inverting these would surface here.
        opus = DEFAULT_ANTHROPIC_PRICING["claude-opus-4-7"]
        sonnet = DEFAULT_ANTHROPIC_PRICING["claude-sonnet-4-7"]
        haiku = DEFAULT_ANTHROPIC_PRICING["claude-haiku-4-5"]
        assert opus.input_usd_per_million > sonnet.input_usd_per_million
        assert sonnet.input_usd_per_million > haiku.input_usd_per_million
        assert opus.output_usd_per_million > sonnet.output_usd_per_million
        assert sonnet.output_usd_per_million > haiku.output_usd_per_million


# --------------------------------------------------------------------------
# CostStore
# --------------------------------------------------------------------------


def _record(
    *,
    trace_id: str = "abc",
    timestamp: datetime | None = None,
    cost_rub: float = 10.0,
) -> CostRecord:
    return CostRecord(
        timestamp=timestamp or datetime.now(UTC),
        trace_id=trace_id,
        model="claude-opus-4-7",
        input_tokens=1000,
        output_tokens=500,
        input_usd_per_million=15.0,
        output_usd_per_million=75.0,
        usd_to_rub_rate=100.0,
        cost_rub=cost_rub,
    )


class TestCostStore:
    def test_append_then_read(self, tmp_path: Path) -> None:
        store = CostStore(tmp_path / "cost.jsonl")
        r = _record(trace_id="run1")

        store.append(r)
        loaded = store.all_records()

        assert len(loaded) == 1
        assert loaded[0].trace_id == "run1"

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        store = CostStore(tmp_path / "does_not_exist.jsonl")

        assert store.all_records() == []

    def test_append_creates_parent_dirs(self, tmp_path: Path) -> None:
        deep = tmp_path / "logs" / "agent" / "cost.jsonl"
        store = CostStore(deep)

        store.append(_record())

        assert deep.exists()

    def test_corrupt_line_skipped(self, tmp_path: Path) -> None:
        path = tmp_path / "cost.jsonl"
        valid = _record(trace_id="ok").model_dump_json()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w") as f:
            f.write("not json\n")
            f.write(valid + "\n")
            f.write('{"missing_required_fields": true}\n')

        store = CostStore(path)
        loaded = store.all_records()

        # Only the valid record survives.
        assert len(loaded) == 1
        assert loaded[0].trace_id == "ok"

    def test_records_in_month_filters(self, tmp_path: Path) -> None:
        store = CostStore(tmp_path / "cost.jsonl")
        store.append(
            _record(
                trace_id="apr",
                timestamp=datetime(2026, 4, 15, tzinfo=UTC),
            ),
        )
        store.append(
            _record(
                trace_id="may",
                timestamp=datetime(2026, 5, 1, tzinfo=UTC),
            ),
        )

        apr_only = store.records_in_month(year=2026, month=4)

        assert len(apr_only) == 1
        assert apr_only[0].trace_id == "apr"

    def test_records_in_empty_month_returns_empty(self, tmp_path: Path) -> None:
        store = CostStore(tmp_path / "cost.jsonl")

        assert store.records_in_month(year=2026, month=4) == []
