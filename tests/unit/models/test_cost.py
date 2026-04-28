"""Tests for cost-tracking models (M21)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from yadirect_agent.models.cost import (
    CostRecord,
    ModelPricing,
    MonthlyCostSummary,
    aggregate_records,
)


def _record(
    *,
    timestamp: datetime | None = None,
    trace_id: str = "abc123",
    input_tokens: int = 1000,
    output_tokens: int = 500,
    cost_rub: float = 10.0,
) -> CostRecord:
    return CostRecord(
        timestamp=timestamp or datetime.now(UTC),
        trace_id=trace_id,
        model="claude-opus-4-7",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=0,
        input_usd_per_million=15.0,
        output_usd_per_million=75.0,
        cached_input_usd_per_million=1.5,
        usd_to_rub_rate=100.0,
        cost_rub=cost_rub,
    )


class TestModelPricing:
    def test_valid_pricing(self) -> None:
        p = ModelPricing(
            model="claude-opus-4-7",
            input_usd_per_million=15.0,
            output_usd_per_million=75.0,
        )

        assert p.model == "claude-opus-4-7"
        assert p.input_usd_per_million == pytest.approx(15.0)
        assert p.cached_input_usd_per_million == pytest.approx(0.0)

    def test_negative_price_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ModelPricing(
                model="x",
                input_usd_per_million=-1.0,
                output_usd_per_million=10.0,
            )


class TestCostRecord:
    def test_minimal_construction(self) -> None:
        r = _record()
        assert r.trace_id == "abc123"
        assert r.cost_rub == pytest.approx(10.0)

    def test_whitespace_trace_id_rejected(self) -> None:
        # Same hardening as OperationPlan.plan_id and Rationale.decision_id
        # — trace_id must not contain whitespace.
        with pytest.raises(ValidationError, match="whitespace"):
            CostRecord(
                trace_id="has spaces",
                model="x",
                input_tokens=1,
                output_tokens=1,
                input_usd_per_million=1,
                output_usd_per_million=1,
                usd_to_rub_rate=100,
                cost_rub=1,
            )

    def test_negative_token_count_rejected(self) -> None:
        with pytest.raises(ValidationError):
            CostRecord(
                trace_id="x",
                model="y",
                input_tokens=-1,
                output_tokens=0,
                input_usd_per_million=1,
                output_usd_per_million=1,
                usd_to_rub_rate=100,
                cost_rub=0,
            )

    def test_nan_inf_rate_rejected(self) -> None:
        # auditor M15.5.1 MEDIUM-2 pattern — IEEE-754 specials in
        # float fields would crash json.dumps in the JSONL store.
        import math

        for bad in [math.nan, math.inf, -math.inf]:
            with pytest.raises(ValidationError):
                CostRecord(
                    trace_id="x",
                    model="y",
                    input_tokens=1,
                    output_tokens=1,
                    input_usd_per_million=1,
                    output_usd_per_million=1,
                    usd_to_rub_rate=bad,
                    cost_rub=1,
                )

    def test_zero_rate_rejected(self) -> None:
        # Field constraint gt=0 — a zero rate would zero out every
        # cost_rub forever, hiding spend from cost status.
        with pytest.raises(ValidationError):
            CostRecord(
                trace_id="x",
                model="y",
                input_tokens=1,
                output_tokens=1,
                input_usd_per_million=1,
                output_usd_per_million=1,
                usd_to_rub_rate=0,
                cost_rub=0,
            )

    def test_round_trips_through_json(self) -> None:
        original = _record()
        as_json = original.model_dump_json()
        loaded = CostRecord.model_validate_json(as_json)

        assert loaded.trace_id == original.trace_id
        assert loaded.model == original.model
        assert loaded.cost_rub == pytest.approx(original.cost_rub)

    def test_extra_field_ignored_for_forward_compat(self) -> None:
        # Same JSONL-archive concern as Rationale (auditor M20 LOW-5):
        # a future agent version adding a field shouldn't cause silent
        # record loss when an older binary reads the file.
        loaded = CostRecord.model_validate(
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "trace_id": "abc",
                "model": "x",
                "input_tokens": 1,
                "output_tokens": 1,
                "input_usd_per_million": 1,
                "output_usd_per_million": 1,
                "usd_to_rub_rate": 100,
                "cost_rub": 1,
                "future_field_v0_3_0": "hi",
            },
        )
        assert loaded.trace_id == "abc"


class TestAggregateRecords:
    def test_empty_returns_empty(self) -> None:
        assert aggregate_records([]) == {}

    def test_single_month_aggregate(self) -> None:
        ts = datetime(2026, 4, 15, 12, 0, tzinfo=UTC)
        records = [
            _record(
                timestamp=ts,
                trace_id="run1",
                input_tokens=1000,
                output_tokens=500,
                cost_rub=10.0,
            ),
            _record(
                timestamp=ts,
                trace_id="run2",
                input_tokens=2000,
                output_tokens=200,
                cost_rub=5.0,
            ),
        ]

        agg = aggregate_records(records)

        assert (2026, 4) in agg
        summary = agg[(2026, 4)]
        assert summary.total_input_tokens == 3000
        assert summary.total_output_tokens == 700
        assert summary.total_cost_rub == pytest.approx(15.0)
        assert summary.run_count == 2

    def test_multiple_calls_one_run_count_as_one_run(self) -> None:
        # An agent run that issues multiple messages.create calls
        # (one per tool-use turn) shares the same trace_id. The
        # run_count must reflect distinct runs, not raw call count.
        ts = datetime(2026, 4, 15, 12, 0, tzinfo=UTC)
        records = [
            _record(timestamp=ts, trace_id="run1", cost_rub=3.0),
            _record(timestamp=ts, trace_id="run1", cost_rub=4.0),
            _record(timestamp=ts, trace_id="run1", cost_rub=5.0),
        ]

        agg = aggregate_records(records)

        assert agg[(2026, 4)].run_count == 1
        assert agg[(2026, 4)].total_cost_rub == pytest.approx(12.0)

    def test_buckets_by_month(self) -> None:
        ts_apr = datetime(2026, 4, 30, 23, 59, tzinfo=UTC)
        ts_may = datetime(2026, 5, 1, 0, 1, tzinfo=UTC)
        records = [
            _record(timestamp=ts_apr, trace_id="apr", cost_rub=10.0),
            _record(timestamp=ts_may, trace_id="may", cost_rub=20.0),
        ]

        agg = aggregate_records(records)

        assert (2026, 4) in agg
        assert (2026, 5) in agg
        assert agg[(2026, 4)].total_cost_rub == pytest.approx(10.0)
        assert agg[(2026, 5)].total_cost_rub == pytest.approx(20.0)


class TestMonthlyCostSummary:
    def test_construction(self) -> None:
        s = MonthlyCostSummary(
            year=2026,
            month=4,
            total_input_tokens=10000,
            total_output_tokens=2000,
            total_cost_rub=150.5,
            run_count=5,
        )
        assert s.year == 2026
        assert s.month == 4

    def test_invalid_month_rejected(self) -> None:
        with pytest.raises(ValidationError):
            MonthlyCostSummary(
                year=2026,
                month=13,
                total_input_tokens=0,
                total_output_tokens=0,
                total_cost_rub=0,
                run_count=0,
            )
