"""Tests for Metrika-related models.

Trust pydantic for field-presence; we test the *invariants we wrote*:
- ``DateRange`` rejects end < start at construction
- ``DateRange.to_metrika_strings`` produces ISO-8601 day strings
- ``CampaignPerformance`` is frozen (mutation raises)
- ``MetrikaGoal`` and ``ReportRow`` accept extra fields (forward-compat)
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import date

import pytest

from yadirect_agent.models.metrika import (
    CampaignPerformance,
    DateRange,
    MetrikaGoal,
    ReportRow,
)


class TestDateRange:
    def test_valid_range_constructs(self) -> None:
        r = DateRange(start=date(2026, 4, 1), end=date(2026, 4, 7))

        assert r.start == date(2026, 4, 1)
        assert r.end == date(2026, 4, 7)

    def test_single_day_is_valid(self) -> None:
        # Same-day range is a one-day window, not an error.
        r = DateRange(start=date(2026, 4, 1), end=date(2026, 4, 1))

        assert r.start == r.end

    def test_end_before_start_rejected(self) -> None:
        with pytest.raises(ValueError, match="before start"):
            DateRange(start=date(2026, 4, 10), end=date(2026, 4, 1))

    def test_to_metrika_strings_iso_format(self) -> None:
        r = DateRange(start=date(2026, 4, 1), end=date(2026, 4, 7))

        date1, date2 = r.to_metrika_strings()

        assert date1 == "2026-04-01"
        assert date2 == "2026-04-07"


class TestCampaignPerformance:
    def test_construction_carries_all_fields(self) -> None:
        perf = CampaignPerformance(
            campaign_id=42,
            campaign_name="brand",
            date_range=DateRange(start=date(2026, 4, 1), end=date(2026, 4, 7)),
            clicks=120,
            cost_rub=850.50,
            conversions=5,
            cpa_rub=170.10,
            cr_pct=4.17,
        )

        assert perf.campaign_id == 42
        assert perf.cost_rub == pytest.approx(850.50)
        assert perf.cpa_rub == pytest.approx(170.10)

    def test_frozen_blocks_mutation(self) -> None:
        perf = CampaignPerformance(
            campaign_id=42,
            campaign_name="brand",
            date_range=DateRange(start=date(2026, 4, 1), end=date(2026, 4, 7)),
            clicks=120,
            cost_rub=850.50,
            conversions=5,
            cpa_rub=170.10,
            cr_pct=4.17,
        )

        with pytest.raises(FrozenInstanceError):
            perf.cost_rub = 999.0  # type: ignore[misc]

    def test_zero_conversions_carries_none_cpa(self) -> None:
        # The model itself doesn't enforce this — it just types it.
        # The service is responsible for setting None on zero conversions;
        # the test pins that None is acceptable at the dataclass layer.
        perf = CampaignPerformance(
            campaign_id=42,
            campaign_name="brand",
            date_range=DateRange(start=date(2026, 4, 1), end=date(2026, 4, 7)),
            clicks=120,
            cost_rub=2400.0,
            conversions=0,
            cpa_rub=None,
            cr_pct=0.0,
        )

        assert perf.cpa_rub is None


class TestMetrikaGoal:
    def test_minimal_construction(self) -> None:
        g = MetrikaGoal(id=12345, name="Order completed", type="number")

        assert g.id == 12345
        assert g.name == "Order completed"
        assert g.type == "number"

    def test_unknown_goal_type_accepted_as_raw_string(self) -> None:
        # Forward-compat: Metrika may add new goal types; we don't crash.
        g = MetrikaGoal(id=1, name="x", type="some_future_type")

        assert g.type == "some_future_type"

    def test_extra_fields_preserved(self) -> None:
        # ConfigDict(extra="allow") so a new field doesn't break parsing.
        g = MetrikaGoal.model_validate(
            {"id": 1, "name": "x", "type": "number", "default_price": 500},
        )

        assert g.id == 1
        # extra="allow" exposes unknown fields via __pydantic_extra__
        assert g.model_extra is not None
        assert g.model_extra["default_price"] == 500


class TestReportRow:
    def test_full_row_parses(self) -> None:
        row = ReportRow.model_validate(
            {
                "dimensions": [{"name": "yandex_direct", "icon_id": 12}],
                "metrics": [120.0, 850.5, 5.0],
            },
        )

        assert row.dimensions == [{"name": "yandex_direct", "icon_id": 12}]
        assert row.metrics == [120.0, 850.5, 5.0]

    def test_empty_row_uses_defaults(self) -> None:
        row = ReportRow.model_validate({})

        assert row.dimensions == []
        assert row.metrics == []
