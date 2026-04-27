"""Tests for ``ReportingService`` (M6 basic).

Focus: the service's *decisions* — what it asks Metrika for, how it
joins cost/clicks/conversions into ``CampaignPerformance``, when it
returns ``cpa_rub=None`` vs a number, and what it raises when the
operator hasn't configured a counter.

We monkeypatch ``MetrikaService`` so HTTP is mocked at the service
boundary, not the wire — this is the same seam ``test_campaigns.py``
uses for ``DirectService``.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Self

import pytest
from structlog.testing import capture_logs

from yadirect_agent.config import Settings
from yadirect_agent.exceptions import ConfigError
from yadirect_agent.models.metrika import DateRange, MetrikaGoal, ReportRow
from yadirect_agent.services import reporting as reporting_module
from yadirect_agent.services.reporting import ReportingService

# --------------------------------------------------------------------------
# In-memory stub that replaces MetrikaService.
# --------------------------------------------------------------------------


class _FakeMetrikaService:
    """Captures calls and replays scripted Metrika responses."""

    def __init__(
        self,
        *,
        report_rows: list[ReportRow] | None = None,
        goals: list[MetrikaGoal] | None = None,
    ) -> None:
        self._report_rows = report_rows or []
        self._goals = goals or []
        self.report_calls: list[dict[str, Any]] = []
        self.goals_calls: list[int] = []

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    async def get_goals(self, *, counter_id: int) -> list[MetrikaGoal]:
        self.goals_calls.append(counter_id)
        return list(self._goals)

    async def get_report(
        self,
        *,
        counter_id: int,
        metrics: list[str],
        dimensions: list[str],
        date_range: DateRange,
        filters: str | None = None,
    ) -> list[ReportRow]:
        self.report_calls.append(
            {
                "counter_id": counter_id,
                "metrics": metrics,
                "dimensions": dimensions,
                "date_range": date_range,
                "filters": filters,
            },
        )
        return list(self._report_rows)


def _patch_metrika(monkeypatch: pytest.MonkeyPatch, fake: _FakeMetrikaService) -> None:
    """Replace ``MetrikaService`` in the reporting module with the fake."""
    monkeypatch.setattr(reporting_module, "MetrikaService", lambda _settings: fake)


def _settings_with_counter(settings: Settings, counter_id: int = 12345) -> Settings:
    """Return a copy of the fixture settings with counter_id set."""
    return settings.model_copy(update={"yandex_metrika_counter_id": counter_id})


_WEEK = DateRange(start=date(2026, 4, 1), end=date(2026, 4, 7))


# --------------------------------------------------------------------------
# campaign_performance
# --------------------------------------------------------------------------


class TestCampaignPerformance:
    async def test_happy_path_with_goal(
        self,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Wire shape: one row with [visits, directCost, goalConversions].
        fake = _FakeMetrikaService(
            report_rows=[
                ReportRow(
                    dimensions=[{"name": "brand", "id": 42}],
                    metrics=[120.0, 850.5, 5.0],
                ),
            ],
        )
        _patch_metrika(monkeypatch, fake)

        async with ReportingService(_settings_with_counter(settings)) as svc:
            perf = await svc.campaign_performance(
                campaign_id=42,
                campaign_name="brand",
                date_range=_WEEK,
                goal_id=100,
            )

        assert perf.campaign_id == 42
        assert perf.campaign_name == "brand"
        assert perf.clicks == 120
        assert perf.cost_rub == pytest.approx(850.5)
        assert perf.conversions == 5
        assert perf.cpa_rub == pytest.approx(170.1)
        # cr_pct = conversions / clicks * 100 = 5/120 * 100 = 4.166...
        assert perf.cr_pct == pytest.approx(5 / 120 * 100, rel=1e-3)

    async def test_happy_path_without_goal_carries_none_conversions(
        self,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # No goal_id passed → service must not ask Metrika for goal metric.
        fake = _FakeMetrikaService(
            report_rows=[
                ReportRow(
                    dimensions=[{"name": "brand", "id": 42}],
                    metrics=[120.0, 850.5],  # only visits + cost, no conversions
                ),
            ],
        )
        _patch_metrika(monkeypatch, fake)

        async with ReportingService(_settings_with_counter(settings)) as svc:
            perf = await svc.campaign_performance(
                campaign_id=42,
                campaign_name="brand",
                date_range=_WEEK,
                goal_id=None,
            )

        assert perf.conversions == 0
        assert perf.cpa_rub is None
        # Metrics requested didn't include any goal field
        called_metrics = fake.report_calls[0]["metrics"]
        assert not any("goal" in m for m in called_metrics)

    async def test_zero_conversions_carries_none_cpa(
        self,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Cost without conversions — the canonical "burning campaign"
        # signal. cpa_rub MUST be None, never 0 or infinity, so a
        # rule-based "kill if cpa > 1000" check doesn't accidentally
        # nuke a campaign with 0 conversions.
        fake = _FakeMetrikaService(
            report_rows=[
                ReportRow(
                    dimensions=[{"name": "brand", "id": 42}],
                    metrics=[80.0, 2400.0, 0.0],
                ),
            ],
        )
        _patch_metrika(monkeypatch, fake)

        async with ReportingService(_settings_with_counter(settings)) as svc:
            perf = await svc.campaign_performance(
                campaign_id=42,
                campaign_name="brand",
                date_range=_WEEK,
                goal_id=100,
            )

        assert perf.conversions == 0
        assert perf.cost_rub == pytest.approx(2400.0)
        assert perf.cpa_rub is None  # never 0, never inf
        assert perf.cr_pct == pytest.approx(0.0)

    async def test_zero_clicks_carries_none_cr(
        self,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Brand-new campaign that hasn't shown yet.
        fake = _FakeMetrikaService(
            report_rows=[
                ReportRow(
                    dimensions=[{"name": "brand", "id": 42}],
                    metrics=[0.0, 0.0, 0.0],
                ),
            ],
        )
        _patch_metrika(monkeypatch, fake)

        async with ReportingService(_settings_with_counter(settings)) as svc:
            perf = await svc.campaign_performance(
                campaign_id=42,
                campaign_name="brand",
                date_range=_WEEK,
                goal_id=100,
            )

        assert perf.clicks == 0
        assert perf.cr_pct is None
        assert perf.cpa_rub is None

    async def test_metrika_returns_no_data_for_campaign(
        self,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # No rows = Metrika has no record of this campaign's traffic in
        # the window (could be brand-new, paused, or fresh counter).
        # Return zero-filled CampaignPerformance instead of crashing.
        fake = _FakeMetrikaService(report_rows=[])
        _patch_metrika(monkeypatch, fake)

        async with ReportingService(_settings_with_counter(settings)) as svc:
            perf = await svc.campaign_performance(
                campaign_id=42,
                campaign_name="brand",
                date_range=_WEEK,
                goal_id=100,
            )

        assert perf.clicks == 0
        assert perf.cost_rub == 0.0
        assert perf.conversions == 0
        assert perf.cpa_rub is None
        assert perf.cr_pct is None

    async def test_filter_targets_specific_campaign(
        self,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Filter must reference ym:ad:directCampaignID==<id>; without
        # it, Metrika would return account-level totals instead of
        # campaign-level — silently inflating every campaign's data.
        fake = _FakeMetrikaService(report_rows=[])
        _patch_metrika(monkeypatch, fake)

        async with ReportingService(_settings_with_counter(settings)) as svc:
            await svc.campaign_performance(
                campaign_id=42,
                campaign_name="brand",
                date_range=_WEEK,
                goal_id=100,
            )

        call = fake.report_calls[0]
        assert call["filters"] is not None
        assert "ym:ad:directCampaignID" in call["filters"]
        assert "42" in call["filters"]

    async def test_missing_counter_id_raises_config_error(
        self,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Settings.yandex_metrika_counter_id is None — agent isn't fully
        # configured. We should fail fast with a clear pointer at the
        # right env var, not silently call get_report(counter_id=None)
        # and crash on a 400.
        _patch_metrika(monkeypatch, _FakeMetrikaService())

        async with ReportingService(settings) as svc:  # counter_id stays None
            with pytest.raises(ConfigError, match="counter"):
                await svc.campaign_performance(
                    campaign_id=42,
                    campaign_name="brand",
                    date_range=_WEEK,
                    goal_id=100,
                )


# --------------------------------------------------------------------------
# account_overview
# --------------------------------------------------------------------------


class TestAccountOverview:
    async def test_returns_one_perf_per_campaign(
        self,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Three campaigns, each as a separate row grouped by
        # directCampaignID. Each dimension dict carries the campaign
        # id (int) and name (string).
        fake = _FakeMetrikaService(
            report_rows=[
                ReportRow(
                    dimensions=[{"id": 42, "name": "brand"}],
                    metrics=[120.0, 850.5, 5.0],
                ),
                ReportRow(
                    dimensions=[{"id": 51, "name": "non-brand"}],
                    metrics=[80.0, 2400.0, 0.0],
                ),
                ReportRow(
                    dimensions=[{"id": 73, "name": "retargeting"}],
                    metrics=[45.0, 350.0, 8.0],
                ),
            ],
        )
        _patch_metrika(monkeypatch, fake)

        async with ReportingService(_settings_with_counter(settings)) as svc:
            results = await svc.account_overview(date_range=_WEEK, goal_id=100)

        # All three campaigns produce a performance row.
        assert len(results) == 3
        ids = sorted(p.campaign_id for p in results)
        assert ids == [42, 51, 73]

        # Find the burning campaign (51) explicitly by id and check
        # the cpa-None contract under cost>0, conversions=0.
        burning = next(p for p in results if p.campaign_id == 51)
        assert burning.cost_rub == pytest.approx(2400.0)
        assert burning.conversions == 0
        assert burning.cpa_rub is None  # critical: never 0/inf
        assert burning.cr_pct == pytest.approx(0.0)

    async def test_groups_by_campaign_id_dimension(
        self,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Service must request the directCampaignID dimension (numeric
        # id is the join key — by-name would conflate campaigns sharing
        # a name, which legitimately happens for promo cycles).
        fake = _FakeMetrikaService(report_rows=[])
        _patch_metrika(monkeypatch, fake)

        async with ReportingService(_settings_with_counter(settings)) as svc:
            await svc.account_overview(date_range=_WEEK, goal_id=100)

        call = fake.report_calls[0]
        assert "ym:ad:directCampaignID" in call["dimensions"]

    async def test_no_filter_at_account_level(
        self,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # account_overview is intentionally unfiltered — we want every
        # campaign with traffic in the window. Adding a filter here
        # would silently shrink the report.
        fake = _FakeMetrikaService(report_rows=[])
        _patch_metrika(monkeypatch, fake)

        async with ReportingService(_settings_with_counter(settings)) as svc:
            await svc.account_overview(date_range=_WEEK, goal_id=100)

        call = fake.report_calls[0]
        assert call["filters"] is None

    async def test_without_goal_id_omits_conversion_metric(
        self,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fake = _FakeMetrikaService(
            report_rows=[
                ReportRow(
                    dimensions=[{"id": 42, "name": "brand"}],
                    metrics=[120.0, 850.5],  # no conversions metric
                ),
            ],
        )
        _patch_metrika(monkeypatch, fake)

        async with ReportingService(_settings_with_counter(settings)) as svc:
            results = await svc.account_overview(date_range=_WEEK, goal_id=None)

        assert len(results) == 1
        assert results[0].conversions == 0
        assert results[0].cpa_rub is None
        # And the request didn't ask for a goal metric.
        called_metrics = fake.report_calls[0]["metrics"]
        assert not any("goal" in m for m in called_metrics)

    async def test_skips_rows_with_malformed_dimensions(
        self,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Defensive: if Metrika returns a row missing the campaign id
        # (shouldn't happen in practice, but the wire is the wire),
        # skip it rather than crashing the whole overview. The skip
        # must emit a structured warning so silent data loss in the
        # overview is observable. (auditor M6 HIGH-2.)
        fake = _FakeMetrikaService(
            report_rows=[
                ReportRow(
                    dimensions=[{"id": 42, "name": "brand"}],
                    metrics=[120.0, 850.5, 5.0],
                ),
                ReportRow(
                    dimensions=[{}],  # broken — no id, no name
                    metrics=[80.0, 2400.0, 0.0],
                ),
                ReportRow(
                    dimensions=[{"id": "not-an-int", "name": "weird"}],
                    metrics=[45.0, 350.0, 8.0],
                ),
            ],
        )
        _patch_metrika(monkeypatch, fake)

        with capture_logs() as captured_logs:
            async with ReportingService(_settings_with_counter(settings)) as svc:
                results = await svc.account_overview(date_range=_WEEK, goal_id=100)

        # Only the well-formed row survives; we don't fabricate ids.
        assert len(results) == 1
        assert results[0].campaign_id == 42

        # Two malformed rows produce two warnings, each with the
        # specific shape that triggered the skip. Without these the
        # operator can't tell that account_overview silently dropped
        # data when Metrika changes wire shape.
        warnings = [
            log
            for log in captured_logs
            if log["log_level"] == "warning" and log["event"].startswith("metrika.row.dimension")
        ]
        assert len(warnings) == 2

    async def test_non_ascii_digit_id_skipped_not_crashed(
        self,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Non-ASCII digit characters (U+00B2 superscript-two,
        # Arabic-Indic digits, etc.) make ``str.isdigit()`` return True
        # but then ``int()`` raises ValueError. The previous code
        # would crash the whole overview on such a row; we must skip
        # gracefully and keep going. (auditor M6 MEDIUM-4.)
        fake = _FakeMetrikaService(
            report_rows=[
                ReportRow(
                    dimensions=[{"id": "²", "name": "weird-superscript"}],
                    metrics=[10.0, 100.0, 1.0],
                ),
                ReportRow(
                    dimensions=[{"id": 42, "name": "brand"}],
                    metrics=[120.0, 850.5, 5.0],
                ),
            ],
        )
        _patch_metrika(monkeypatch, fake)

        async with ReportingService(_settings_with_counter(settings)) as svc:
            results = await svc.account_overview(date_range=_WEEK, goal_id=100)

        assert len(results) == 1
        assert results[0].campaign_id == 42

    async def test_empty_account_returns_empty_list(
        self,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fake = _FakeMetrikaService(report_rows=[])
        _patch_metrika(monkeypatch, fake)

        async with ReportingService(_settings_with_counter(settings)) as svc:
            results = await svc.account_overview(date_range=_WEEK, goal_id=100)

        assert results == []

    async def test_missing_counter_id_raises_config_error(
        self,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _patch_metrika(monkeypatch, _FakeMetrikaService())

        async with ReportingService(settings) as svc:  # counter_id stays None
            with pytest.raises(ConfigError, match="counter"):
                await svc.account_overview(date_range=_WEEK, goal_id=100)
