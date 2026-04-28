"""Tests for ``HealthCheckService`` (M15.5.1).

The service consumes ``CampaignPerformance`` rows from the M6
``ReportingService.account_overview`` and applies rule-based checks.
We monkeypatch ``ReportingService`` here for the same reason
``test_reporting.py`` monkeypatches ``MetrikaService``: testing
*decisions* at this layer, not the wire shape underneath.

This file pins the contract for:
- the service scaffold (constructor, async-context, `run_account_check`);
- the burning-campaign rule.

A second file or section below adds the high-CPA rule.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Self

import pytest

from yadirect_agent.config import Settings
from yadirect_agent.models.health import Severity
from yadirect_agent.models.metrika import CampaignPerformance, DateRange
from yadirect_agent.services import health_check as health_check_module
from yadirect_agent.services.health_check import HealthCheckService

# --------------------------------------------------------------------------
# Fakes.
# --------------------------------------------------------------------------


class _FakeReportingService:
    """In-memory replacement for ``ReportingService``."""

    def __init__(self, *, overview: list[CampaignPerformance] | None = None) -> None:
        self._overview = overview or []
        self.calls: list[dict[str, Any]] = []

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    async def account_overview(
        self,
        *,
        date_range: DateRange,
        goal_id: int | None = None,
    ) -> list[CampaignPerformance]:
        self.calls.append({"date_range": date_range, "goal_id": goal_id})
        return list(self._overview)


def _patch_reporting(
    monkeypatch: pytest.MonkeyPatch,
    fake: _FakeReportingService,
) -> None:
    monkeypatch.setattr(
        health_check_module,
        "ReportingService",
        lambda _settings: fake,
    )


_WEEK = DateRange(start=date(2026, 4, 1), end=date(2026, 4, 7))


def _perf(
    *,
    campaign_id: int,
    name: str = "test",
    clicks: int = 100,
    cost_rub: float = 500.0,
    conversions: int = 5,
    cpa_rub: float | None = 100.0,
    cr_pct: float | None = 5.0,
) -> CampaignPerformance:
    return CampaignPerformance(
        campaign_id=campaign_id,
        campaign_name=name,
        date_range=_WEEK,
        clicks=clicks,
        cost_rub=cost_rub,
        conversions=conversions,
        cpa_rub=cpa_rub,
        cr_pct=cr_pct,
    )


# --------------------------------------------------------------------------
# Service scaffold.
# --------------------------------------------------------------------------


class TestHealthCheckServiceScaffold:
    async def test_returns_empty_report_for_empty_account(
        self,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _patch_reporting(monkeypatch, _FakeReportingService(overview=[]))

        async with HealthCheckService(settings) as svc:
            report = await svc.run_account_check(date_range=_WEEK)

        assert report.findings == []
        assert report.has_findings is False
        assert report.date_range == _WEEK

    async def test_passes_date_range_and_goal_id_to_reporting(
        self,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The service must forward the date range and goal_id to the
        # underlying reporting service unchanged. Without this, a
        # caller asking "show me the last 30 days against goal 100"
        # would silently get the default window or no goal.
        fake = _FakeReportingService(overview=[])
        _patch_reporting(monkeypatch, fake)

        async with HealthCheckService(settings) as svc:
            await svc.run_account_check(date_range=_WEEK, goal_id=100)

        assert len(fake.calls) == 1
        assert fake.calls[0]["date_range"] == _WEEK
        assert fake.calls[0]["goal_id"] == 100

    async def test_returns_report_with_correct_date_range(
        self,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        custom = DateRange(start=date(2026, 3, 1), end=date(2026, 3, 31))
        _patch_reporting(monkeypatch, _FakeReportingService(overview=[]))

        async with HealthCheckService(settings) as svc:
            report = await svc.run_account_check(date_range=custom)

        assert report.date_range == custom


# --------------------------------------------------------------------------
# BurningCampaignRule.
# --------------------------------------------------------------------------


class TestBurningCampaignRule:
    async def test_flags_campaign_with_cost_and_zero_conversions(
        self,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Canonical burning campaign: 2400 RUB spent, 0 conversions,
        # cpa_rub is None per M6 contract. Must produce a HIGH-severity
        # finding with the spent amount as the estimated impact.
        burning = _perf(
            campaign_id=51,
            name="non-brand",
            clicks=80,
            cost_rub=2400.0,
            conversions=0,
            cpa_rub=None,
            cr_pct=0.0,
        )
        _patch_reporting(monkeypatch, _FakeReportingService(overview=[burning]))

        async with HealthCheckService(settings) as svc:
            report = await svc.run_account_check(date_range=_WEEK, goal_id=100)

        assert len(report.findings) == 1
        finding = report.findings[0]
        assert finding.rule_id == "burning_campaign"
        assert finding.severity == Severity.HIGH
        assert finding.campaign_id == 51
        assert finding.campaign_name == "non-brand"
        assert finding.estimated_impact_rub == pytest.approx(2400.0)
        assert "non-brand" in finding.message
        assert "2400" in finding.message or "2,400" in finding.message
        assert "0 conversions" in finding.message

    async def test_does_not_flag_campaign_with_conversions(
        self,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Spent money + got conversions = healthy. Must NOT fire even
        # though cost is high.
        healthy = _perf(
            campaign_id=42,
            name="brand",
            clicks=120,
            cost_rub=850.0,
            conversions=5,
            cpa_rub=170.0,
        )
        _patch_reporting(monkeypatch, _FakeReportingService(overview=[healthy]))

        async with HealthCheckService(settings) as svc:
            report = await svc.run_account_check(date_range=_WEEK, goal_id=100)

        assert report.findings == []

    async def test_does_not_flag_campaign_with_zero_cost(
        self,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # No cost, no conversions = nothing happened. Not "burning".
        # If we flagged this, paused / new campaigns would all show
        # up as alerts — pure noise.
        idle = _perf(
            campaign_id=99,
            name="paused",
            clicks=0,
            cost_rub=0.0,
            conversions=0,
            cpa_rub=None,
            cr_pct=None,
        )
        _patch_reporting(monkeypatch, _FakeReportingService(overview=[idle]))

        async with HealthCheckService(settings) as svc:
            report = await svc.run_account_check(date_range=_WEEK, goal_id=100)

        assert report.findings == []

    async def test_does_not_flag_below_min_burn_threshold(
        self,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Tiny spend with zero conversions is statistically nothing —
        # 1 RUB on a 7-day window with 0 conversions is not a real
        # signal. Threshold is configurable via the rule constant.
        # This pins the contract that micro-spend doesn't pollute the
        # report.
        tiny = _perf(
            campaign_id=12,
            name="micro",
            clicks=2,
            cost_rub=15.0,  # below default threshold
            conversions=0,
            cpa_rub=None,
            cr_pct=0.0,
        )
        _patch_reporting(monkeypatch, _FakeReportingService(overview=[tiny]))

        async with HealthCheckService(settings) as svc:
            report = await svc.run_account_check(date_range=_WEEK, goal_id=100)

        assert report.findings == []

    async def test_skips_when_no_goal_provided(
        self,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Without goal_id, conversions are always 0 in account_overview
        # by construction (M6 contract). Firing the burning rule on
        # that would flag every campaign — false positives by design.
        # The rule must skip when goal_id is None.
        looks_burning = _perf(
            campaign_id=51,
            name="non-brand",
            clicks=80,
            cost_rub=2400.0,
            conversions=0,
            cpa_rub=None,
            cr_pct=0.0,
        )
        _patch_reporting(monkeypatch, _FakeReportingService(overview=[looks_burning]))

        async with HealthCheckService(settings) as svc:
            report = await svc.run_account_check(date_range=_WEEK, goal_id=None)

        assert report.findings == []

    async def test_flags_multiple_burning_campaigns(
        self,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        burning1 = _perf(
            campaign_id=51,
            name="non-brand",
            clicks=80,
            cost_rub=2400.0,
            conversions=0,
            cpa_rub=None,
        )
        healthy = _perf(campaign_id=42, name="brand", conversions=5, cpa_rub=170.0)
        burning2 = _perf(
            campaign_id=73,
            name="retargeting",
            clicks=30,
            cost_rub=600.0,
            conversions=0,
            cpa_rub=None,
        )
        _patch_reporting(
            monkeypatch,
            _FakeReportingService(overview=[burning1, healthy, burning2]),
        )

        async with HealthCheckService(settings) as svc:
            report = await svc.run_account_check(date_range=_WEEK, goal_id=100)

        burning_ids = sorted(
            f.campaign_id for f in report.findings if f.rule_id == "burning_campaign"
        )
        assert burning_ids == [51, 73]


# --------------------------------------------------------------------------
# HighCpaRule.
# --------------------------------------------------------------------------


def _settings_with_target(settings: Settings, target_cpa: float) -> Settings:
    """Settings copy with account_target_cpa_rub set."""
    return settings.model_copy(update={"account_target_cpa_rub": target_cpa})


class TestHighCpaRule:
    async def test_flags_campaign_above_target_cpa(
        self,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Target 600 RUB; campaign at 1200 = 2× target with enough
        # conversions to be statistically meaningful. Must produce a
        # WARNING-severity finding (not HIGH — the campaign is converting,
        # just expensively).
        expensive = _perf(
            campaign_id=51,
            name="non-brand",
            clicks=200,
            cost_rub=12000.0,
            conversions=10,
            cpa_rub=1200.0,
            cr_pct=5.0,
        )
        _patch_reporting(monkeypatch, _FakeReportingService(overview=[expensive]))

        async with HealthCheckService(_settings_with_target(settings, 600.0)) as svc:
            report = await svc.run_account_check(date_range=_WEEK, goal_id=100)

        high_cpa_findings = [f for f in report.findings if f.rule_id == "high_cpa"]
        assert len(high_cpa_findings) == 1
        finding = high_cpa_findings[0]
        assert finding.severity == Severity.WARNING
        assert finding.campaign_id == 51
        assert finding.campaign_name == "non-brand"
        # Estimated impact = excess cost over target = 10 × (1200 - 600) = 6000
        assert finding.estimated_impact_rub == pytest.approx(6000.0)
        assert "1200" in finding.message
        assert "600" in finding.message  # the target

    async def test_does_not_flag_within_target(
        self,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        on_target = _perf(
            campaign_id=42,
            name="brand",
            clicks=120,
            cost_rub=850.0,
            conversions=5,
            cpa_rub=170.0,
        )
        _patch_reporting(monkeypatch, _FakeReportingService(overview=[on_target]))

        async with HealthCheckService(_settings_with_target(settings, 600.0)) as svc:
            report = await svc.run_account_check(date_range=_WEEK, goal_id=100)

        high_cpa_findings = [f for f in report.findings if f.rule_id == "high_cpa"]
        assert high_cpa_findings == []

    async def test_skips_when_target_not_configured(
        self,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Settings.account_target_cpa_rub is None (default). The rule
        # MUST silently skip — firing on every campaign because we
        # don't know the target would be pure noise.
        expensive = _perf(
            campaign_id=51,
            name="non-brand",
            clicks=200,
            cost_rub=12000.0,
            conversions=10,
            cpa_rub=1200.0,
        )
        _patch_reporting(monkeypatch, _FakeReportingService(overview=[expensive]))

        async with HealthCheckService(settings) as svc:  # no target
            report = await svc.run_account_check(date_range=_WEEK, goal_id=100)

        high_cpa_findings = [f for f in report.findings if f.rule_id == "high_cpa"]
        assert high_cpa_findings == []

    async def test_skips_below_min_conversions(
        self,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Statistical-significance gate: a campaign with 1 conversion
        # at "1200 RUB CPA" might just be unlucky variance. Below the
        # min_conversions threshold the rule treats CPA as noise and
        # skips. Without this, every brand-new campaign with 1
        # conversion would trip immediately.
        too_few_conversions = _perf(
            campaign_id=51,
            name="new",
            clicks=15,
            cost_rub=2400.0,
            conversions=2,  # below default min_conversions
            cpa_rub=1200.0,
        )
        _patch_reporting(
            monkeypatch,
            _FakeReportingService(overview=[too_few_conversions]),
        )

        async with HealthCheckService(_settings_with_target(settings, 600.0)) as svc:
            report = await svc.run_account_check(date_range=_WEEK, goal_id=100)

        high_cpa_findings = [f for f in report.findings if f.rule_id == "high_cpa"]
        assert high_cpa_findings == []

    async def test_skips_when_cpa_unknown(
        self,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # cpa_rub is None per M6 contract on zero conversions / zero
        # cost. The high-CPA rule MUST treat None as "unknown — skip",
        # never as "infinity > target — flag". This is the contract
        # M6 was written against; a regression here would silently
        # nuke every burning campaign as "high CPA" instead of
        # letting BurningCampaignRule produce the right HIGH finding.
        zero_conv = _perf(
            campaign_id=51,
            name="burning",
            clicks=80,
            cost_rub=2400.0,
            conversions=0,
            cpa_rub=None,
        )
        _patch_reporting(monkeypatch, _FakeReportingService(overview=[zero_conv]))

        async with HealthCheckService(_settings_with_target(settings, 600.0)) as svc:
            report = await svc.run_account_check(date_range=_WEEK, goal_id=100)

        high_cpa_findings = [f for f in report.findings if f.rule_id == "high_cpa"]
        # The campaign IS flagged as burning by the other rule,
        # but NOT as high-CPA — those are different conditions.
        assert high_cpa_findings == []
        burning = [f for f in report.findings if f.rule_id == "burning_campaign"]
        assert len(burning) == 1

    async def test_skips_when_no_goal_provided(
        self,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Same gate as burning rule: no goal_id → conversions are 0
        # by construction → CPA is None → can't compare to target.
        looks_expensive = _perf(
            campaign_id=51,
            name="non-brand",
            clicks=200,
            cost_rub=12000.0,
            conversions=10,
            cpa_rub=1200.0,
        )
        _patch_reporting(monkeypatch, _FakeReportingService(overview=[looks_expensive]))

        async with HealthCheckService(_settings_with_target(settings, 600.0)) as svc:
            report = await svc.run_account_check(date_range=_WEEK, goal_id=None)

        high_cpa_findings = [f for f in report.findings if f.rule_id == "high_cpa"]
        assert high_cpa_findings == []
