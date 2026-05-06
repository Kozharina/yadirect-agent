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
from yadirect_agent.models.campaigns import Campaign, CampaignState
from yadirect_agent.models.health import Severity
from yadirect_agent.models.keywords import Keyword
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


@pytest.fixture(autouse=True)
def _autopatch_direct(monkeypatch: pytest.MonkeyPatch) -> None:
    """Autouse: replace ``DirectService`` with an empty fake by default.

    M15.5.2-3 made ``HealthCheckService.run_account_check`` always
    call ``DirectService`` (after the perf-rule loop, for the
    direct-state rules). Without this autouse patch, every existing
    perf-rule test would hit a real ``DirectService`` constructor →
    AuthError on the OAuth token. The default fake returns no
    campaigns, so the direct-state rules emit zero findings and
    don't perturb the perf-rule assertions. Tests that exercise
    direct-state rules call ``_patch_direct(...)`` after this
    fixture has run; ``monkeypatch.setattr`` is last-write-wins,
    so the explicit patch takes effect.
    """
    monkeypatch.setattr(
        health_check_module,
        "DirectService",
        lambda _settings: _FakeDirectService(campaigns=[]),
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
    impressions: int = 0,
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
        impressions=impressions,
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

    async def test_does_not_flag_at_exactly_min_burn_threshold(
        self,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Boundary case: cost_rub == MIN_BURN_RUB (50.0) with 0
        # conversions is treated as below-threshold noise, NOT flagged.
        # The rule uses ``cost_rub <= MIN_BURN_RUB`` for the skip
        # condition. (auditor M15.5.1 LOW-5: boundary semantics
        # explicitly pinned.)
        at_threshold = _perf(
            campaign_id=12,
            name="exact-threshold",
            clicks=2,
            cost_rub=50.0,
            conversions=0,
            cpa_rub=None,
            cr_pct=0.0,
        )
        _patch_reporting(monkeypatch, _FakeReportingService(overview=[at_threshold]))

        async with HealthCheckService(settings) as svc:
            report = await svc.run_account_check(date_range=_WEEK, goal_id=100)

        assert report.findings == []

    async def test_flags_just_above_min_burn_threshold(
        self,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Boundary case: 1 cent above MIN_BURN_RUB does fire. Together
        # with the at-threshold test above, this pins the boundary
        # semantics — strict greater-than. (auditor M15.5.1 LOW-5.)
        just_above = _perf(
            campaign_id=12,
            name="just-above",
            clicks=2,
            cost_rub=50.01,
            conversions=0,
            cpa_rub=None,
            cr_pct=0.0,
        )
        _patch_reporting(monkeypatch, _FakeReportingService(overview=[just_above]))

        async with HealthCheckService(settings) as svc:
            report = await svc.run_account_check(date_range=_WEEK, goal_id=100)

        assert len(report.findings) == 1
        assert report.findings[0].rule_id == "burning_campaign"

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
        # Target 600 RUB; campaign at 1200 = 2x target with enough
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
        # Estimated impact = excess cost over target = 10 * (1200 - 600) = 6000
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


# --------------------------------------------------------------------------
# Direct-state rules (M15.5.2-3): rejected ads + rejected keywords.
# --------------------------------------------------------------------------
#
# These rules differ from BurningCampaign / HighCpa in their data source —
# Direct API state, not Metrika performance. The base class is
# ``_DirectStateRule`` with an async ``collect_findings(direct, *,
# campaigns)`` method. Tests substitute a ``_FakeDirectService`` for the
# real ``DirectService`` so no HTTP fires.


class _FakeDirectService:
    """In-memory replacement for ``DirectService``.

    Returns whatever the test set up; mirrors the API surface
    ``HealthCheckService`` exercises (``get_campaigns`` +
    ``scan_rejected_ads`` + ``scan_rejected_keywords``). Other
    DirectService methods aren't reachable on the rule path and
    are deliberately absent — adding them would invite tests that
    accidentally couple to call sites that don't exist.
    """

    def __init__(
        self,
        *,
        campaigns: list[Campaign] | None = None,
        rejected_ads_by_campaign: dict[int, list[dict[str, Any]]] | None = None,
        rejected_keywords_by_campaign: dict[int, list[Keyword]] | None = None,
    ) -> None:
        self._campaigns = campaigns or []
        self._rejected_ads = rejected_ads_by_campaign or {}
        self._rejected_keywords = rejected_keywords_by_campaign or {}
        self.scan_ads_calls: list[list[int]] = []
        self.scan_keywords_calls: list[list[int]] = []

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    async def get_campaigns(self, *args: Any, **kwargs: Any) -> list[Campaign]:
        return list(self._campaigns)

    async def scan_rejected_ads(self, *, campaign_ids: list[int]) -> list[dict[str, Any]]:
        self.scan_ads_calls.append(list(campaign_ids))
        out: list[dict[str, Any]] = []
        for cid in campaign_ids:
            out.extend(self._rejected_ads.get(cid, []))
        return out

    async def scan_rejected_keywords(self, *, campaign_ids: list[int]) -> list[Keyword]:
        self.scan_keywords_calls.append(list(campaign_ids))
        out: list[Keyword] = []
        for cid in campaign_ids:
            out.extend(self._rejected_keywords.get(cid, []))
        return out


def _patch_direct(monkeypatch: pytest.MonkeyPatch, fake: _FakeDirectService) -> None:
    monkeypatch.setattr(
        health_check_module,
        "DirectService",
        lambda _settings: fake,
    )


def _campaign(
    *,
    campaign_id: int,
    name: str = "test-campaign",
    state: CampaignState = CampaignState.ON,
) -> Campaign:
    return Campaign(id=campaign_id, name=name, state=state)


def _rejected_ad(
    *,
    ad_id: int,
    campaign_id: int,
    title: str | None = "ad-title",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "Id": ad_id,
        "AdGroupId": 100,
        "CampaignId": campaign_id,
        "Status": "REJECTED",
        "State": "ON",
    }
    if title is not None:
        payload["TextAd"] = {"Title": title}
    return payload


def _rejected_keyword(*, kw_id: int, campaign_id: int, text: str = "купить слона") -> Keyword:
    return Keyword(
        Id=kw_id,
        AdGroupId=100,
        CampaignId=campaign_id,
        Keyword=text,
        State="ON",
        Status="REJECTED",
    )


# --------------------------------------------------------------------------
# RejectedAdsRule.
# --------------------------------------------------------------------------


class TestRejectedAdsRule:
    async def test_no_rejected_ads_emits_no_findings(
        self,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Healthy account: scan returns []; no findings, no noise.
        # Validates the rule short-circuits on empty scan rather than
        # pushing through aggregation logic that would crash on
        # missing keys.
        _patch_reporting(monkeypatch, _FakeReportingService(overview=[]))
        _patch_direct(
            monkeypatch,
            _FakeDirectService(campaigns=[_campaign(campaign_id=7, name="brand")]),
        )

        async with HealthCheckService(settings) as svc:
            report = await svc.run_account_check(date_range=_WEEK)

        rejected_findings = [f for f in report.findings if f.rule_id == "rejected_ads"]
        assert rejected_findings == []

    async def test_single_rejected_ad_emits_high_severity_finding(
        self,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Canonical rejection: one ad in one campaign. Severity HIGH
        # because rejected ads silently lose impressions until the
        # operator notices — same urgency tier as a burning campaign.
        # The message must include the campaign name (operators don't
        # remember IDs) and quote the ad title (helps triage which
        # creative to fix).
        _patch_reporting(monkeypatch, _FakeReportingService(overview=[]))
        _patch_direct(
            monkeypatch,
            _FakeDirectService(
                campaigns=[_campaign(campaign_id=7, name="brand")],
                rejected_ads_by_campaign={
                    7: [_rejected_ad(ad_id=5001, campaign_id=7, title="купите кошку дёшево")],
                },
            ),
        )

        async with HealthCheckService(settings) as svc:
            report = await svc.run_account_check(date_range=_WEEK)

        rejected = [f for f in report.findings if f.rule_id == "rejected_ads"]
        assert len(rejected) == 1
        finding = rejected[0]
        assert finding.severity == Severity.HIGH
        assert finding.campaign_id == 7
        assert finding.campaign_name == "brand"
        assert "brand" in finding.message
        # Title quoted in message so the operator can match it against
        # what they see in Direct without an extra round trip.
        assert "купите кошку" in finding.message

    async def test_multiple_rejected_in_one_campaign_aggregates_to_one_finding(
        self,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Two rejected ads in one campaign → ONE Finding with count
        # in the message (not two separate findings). Rationale: the
        # operator's mental model is "campaign X has issues", not
        # "ad 5001 has an issue, also ad 5002 has an issue". One
        # actionable line per campaign is more usable.
        _patch_reporting(monkeypatch, _FakeReportingService(overview=[]))
        _patch_direct(
            monkeypatch,
            _FakeDirectService(
                campaigns=[_campaign(campaign_id=7, name="brand")],
                rejected_ads_by_campaign={
                    7: [
                        _rejected_ad(ad_id=5001, campaign_id=7, title="ad-one"),
                        _rejected_ad(ad_id=5002, campaign_id=7, title="ad-two"),
                    ],
                },
            ),
        )

        async with HealthCheckService(settings) as svc:
            report = await svc.run_account_check(date_range=_WEEK)

        rejected = [f for f in report.findings if f.rule_id == "rejected_ads"]
        assert len(rejected) == 1
        # Count in message (operator sees "2 ad(s)..."), both titles
        # quoted (under sample-limit so all fit).
        assert "2 ad" in rejected[0].message
        assert "ad-one" in rejected[0].message
        assert "ad-two" in rejected[0].message

    async def test_more_than_sample_limit_truncates_with_count_suffix(
        self,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Five rejected ads, sample limit is 3 → message lists 3 with
        # "+2 more" suffix. Without truncation the message becomes
        # unreadable on long-tail accounts where one moderation event
        # rejects 50+ creatives at once.
        _patch_reporting(monkeypatch, _FakeReportingService(overview=[]))
        ads = [_rejected_ad(ad_id=i, campaign_id=7, title=f"ad-{i}") for i in range(1, 6)]
        _patch_direct(
            monkeypatch,
            _FakeDirectService(
                campaigns=[_campaign(campaign_id=7, name="brand")],
                rejected_ads_by_campaign={7: ads},
            ),
        )

        async with HealthCheckService(settings) as svc:
            report = await svc.run_account_check(date_range=_WEEK)

        [finding] = [f for f in report.findings if f.rule_id == "rejected_ads"]
        assert "5 ad" in finding.message
        # 5 - 3 = 2 ads omitted; pinned token ``+2`` so we don't
        # accidentally drift to ``... and 2 more`` and break aud
        # log-grep regressions.
        assert "+2" in finding.message


# --------------------------------------------------------------------------
# RejectedKeywordsRule.
# --------------------------------------------------------------------------


class TestRejectedKeywordsRule:
    async def test_no_rejected_keywords_emits_no_findings(
        self,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _patch_reporting(monkeypatch, _FakeReportingService(overview=[]))
        _patch_direct(
            monkeypatch,
            _FakeDirectService(campaigns=[_campaign(campaign_id=7, name="brand")]),
        )

        async with HealthCheckService(settings) as svc:
            report = await svc.run_account_check(date_range=_WEEK)

        rejected = [f for f in report.findings if f.rule_id == "rejected_keywords"]
        assert rejected == []

    async def test_single_rejected_keyword_emits_high_severity_finding(
        self,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Symmetric to the ads single-rejection test: one keyword,
        # one campaign, HIGH severity. Message quotes the keyword
        # text so the operator can find it in Direct's keyword editor.
        _patch_reporting(monkeypatch, _FakeReportingService(overview=[]))
        _patch_direct(
            monkeypatch,
            _FakeDirectService(
                campaigns=[_campaign(campaign_id=7, name="brand")],
                rejected_keywords_by_campaign={
                    7: [_rejected_keyword(kw_id=999, campaign_id=7, text="оружие купить")],
                },
            ),
        )

        async with HealthCheckService(settings) as svc:
            report = await svc.run_account_check(date_range=_WEEK)

        rejected = [f for f in report.findings if f.rule_id == "rejected_keywords"]
        assert len(rejected) == 1
        finding = rejected[0]
        assert finding.severity == Severity.HIGH
        assert finding.campaign_id == 7
        assert finding.campaign_name == "brand"
        assert "оружие" in finding.message

    async def test_aggregates_multiple_per_campaign(
        self,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Mirror of the ads aggregation test — one Finding per campaign
        # regardless of count.
        _patch_reporting(monkeypatch, _FakeReportingService(overview=[]))
        _patch_direct(
            monkeypatch,
            _FakeDirectService(
                campaigns=[_campaign(campaign_id=7, name="brand")],
                rejected_keywords_by_campaign={
                    7: [
                        _rejected_keyword(kw_id=1, campaign_id=7, text="kw-one"),
                        _rejected_keyword(kw_id=2, campaign_id=7, text="kw-two"),
                        _rejected_keyword(kw_id=3, campaign_id=7, text="kw-three"),
                    ],
                },
            ),
        )

        async with HealthCheckService(settings) as svc:
            report = await svc.run_account_check(date_range=_WEEK)

        rejected = [f for f in report.findings if f.rule_id == "rejected_keywords"]
        assert len(rejected) == 1
        assert "3 keyword" in rejected[0].message


# --------------------------------------------------------------------------
# Direct-state integration via HealthCheckService.
# --------------------------------------------------------------------------


class TestHealthCheckServiceDirectStateIntegration:
    async def test_archived_campaigns_excluded_from_scan(
        self,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Archived campaigns don't burn budget; their rejected ads /
        # keywords aren't actionable. The service must filter them
        # out BEFORE calling scan_rejected_*, otherwise the rule
        # would emit findings the operator can't act on.
        archived = _campaign(campaign_id=99, name="old", state=CampaignState.ARCHIVED)
        active = _campaign(campaign_id=7, name="brand", state=CampaignState.ON)
        fake = _FakeDirectService(
            campaigns=[archived, active],
            # If the scan IS called with the archived id, the test
            # detects it via scan_ads_calls assertion below.
            rejected_ads_by_campaign={
                99: [_rejected_ad(ad_id=1, campaign_id=99, title="ancient")],
                7: [_rejected_ad(ad_id=2, campaign_id=7, title="current")],
            },
        )
        _patch_reporting(monkeypatch, _FakeReportingService(overview=[]))
        _patch_direct(monkeypatch, fake)

        async with HealthCheckService(settings) as svc:
            report = await svc.run_account_check(date_range=_WEEK)

        # Service called scan_rejected_ads exactly once, with ONLY
        # the active campaign id.
        assert fake.scan_ads_calls == [[7]]
        # And the surviving finding is for the active campaign only.
        rejected = [f for f in report.findings if f.rule_id == "rejected_ads"]
        assert len(rejected) == 1
        assert rejected[0].campaign_id == 7

    async def test_both_direct_rules_run_in_a_single_check(
        self,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # End-to-end: a single ``run_account_check`` triggers both
        # RejectedAdsRule AND RejectedKeywordsRule, plus the
        # existing perf-rule loop continues to work for empty
        # overview. The service must NOT short-circuit on the
        # first direct-state rule.
        fake = _FakeDirectService(
            campaigns=[_campaign(campaign_id=7, name="brand")],
            rejected_ads_by_campaign={
                7: [_rejected_ad(ad_id=5001, campaign_id=7, title="bad-ad")],
            },
            rejected_keywords_by_campaign={
                7: [_rejected_keyword(kw_id=999, campaign_id=7, text="bad-kw")],
            },
        )
        _patch_reporting(monkeypatch, _FakeReportingService(overview=[]))
        _patch_direct(monkeypatch, fake)

        async with HealthCheckService(settings) as svc:
            report = await svc.run_account_check(date_range=_WEEK)

        rule_ids = {f.rule_id for f in report.findings}
        assert "rejected_ads" in rule_ids
        assert "rejected_keywords" in rule_ids


# --------------------------------------------------------------------------
# LowCtrRule (M15.5.4).
# --------------------------------------------------------------------------


class TestLowCtrRule:
    async def test_flags_campaign_with_low_ctr(
        self,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 50 clicks / 50_000 impressions = 0.1% CTR — well below
        # the 0.5% threshold, with enough impressions to be
        # statistically meaningful. Severity WARNING (not HIGH)
        # because low CTR is a creative-iteration signal, not a
        # money-burn alarm — the operator may have legitimate
        # reasons for it (brand campaign, broad-match awareness).
        low_ctr = _perf(
            campaign_id=51,
            name="non-brand",
            clicks=50,
            cost_rub=500.0,
            impressions=50_000,
            conversions=2,
            cpa_rub=250.0,
            cr_pct=4.0,
        )
        _patch_reporting(monkeypatch, _FakeReportingService(overview=[low_ctr]))

        async with HealthCheckService(settings) as svc:
            report = await svc.run_account_check(date_range=_WEEK, goal_id=100)

        low_ctr_findings = [f for f in report.findings if f.rule_id == "low_ctr"]
        assert len(low_ctr_findings) == 1
        finding = low_ctr_findings[0]
        assert finding.severity == Severity.WARNING
        assert finding.campaign_id == 51
        assert finding.campaign_name == "non-brand"
        # Message should include the actual CTR percentage and the
        # raw clicks/impressions split so the operator can quickly
        # decide whether the data is real or sampling noise.
        assert "0.10%" in finding.message or "0.1%" in finding.message
        assert "50" in finding.message
        assert "50000" in finding.message or "50,000" in finding.message

    async def test_does_not_flag_campaign_with_healthy_ctr(
        self,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 1500 clicks / 50_000 impressions = 3% CTR — well above
        # threshold. Must NOT fire even though the campaign has
        # plenty of impressions.
        healthy = _perf(
            campaign_id=42,
            name="brand",
            clicks=1500,
            cost_rub=850.0,
            impressions=50_000,
            conversions=10,
        )
        _patch_reporting(monkeypatch, _FakeReportingService(overview=[healthy]))

        async with HealthCheckService(settings) as svc:
            report = await svc.run_account_check(date_range=_WEEK, goal_id=100)

        low_ctr_findings = [f for f in report.findings if f.rule_id == "low_ctr"]
        assert low_ctr_findings == []

    async def test_skips_campaign_below_min_impressions_threshold(
        self,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 1 click / 100 impressions = 1% CTR but the impressions
        # sample is too small to be meaningful (one click could
        # easily be a bot or a misclick). Statistical-significance
        # gate (MIN_IMPRESSIONS=1000) keeps this kind of noise
        # from drowning the signal. Operator's CLI table shouldn't
        # surface "campaign X has CTR=0% with 5 impressions" — the
        # next day's data could shift the verdict to 60%.
        too_small = _perf(
            campaign_id=99,
            name="new-campaign",
            clicks=0,
            cost_rub=10.0,
            impressions=100,
            conversions=0,
        )
        _patch_reporting(monkeypatch, _FakeReportingService(overview=[too_small]))

        async with HealthCheckService(settings) as svc:
            report = await svc.run_account_check(date_range=_WEEK, goal_id=100)

        low_ctr_findings = [f for f in report.findings if f.rule_id == "low_ctr"]
        assert low_ctr_findings == []

    async def test_skips_campaign_with_zero_impressions(
        self,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Defensive: a campaign with zero impressions (paused, not
        # yet running, no Direct→Metrika linkage) must NOT crash
        # with ZeroDivisionError on ``clicks / impressions``. Same
        # contract as ``BurningCampaignRule``: silent skip is the
        # right behaviour for "no data to act on".
        no_data = _perf(
            campaign_id=200,
            name="paused",
            clicks=0,
            cost_rub=0.0,
            impressions=0,
            conversions=0,
            cpa_rub=None,
            cr_pct=None,
        )
        _patch_reporting(monkeypatch, _FakeReportingService(overview=[no_data]))

        async with HealthCheckService(settings) as svc:
            report = await svc.run_account_check(date_range=_WEEK, goal_id=100)

        low_ctr_findings = [f for f in report.findings if f.rule_id == "low_ctr"]
        assert low_ctr_findings == []

    async def test_does_not_require_goal_id(
        self,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Unlike BurningCampaignRule and HighCpaRule, LowCtrRule
        # works without conversions data — CTR is purely
        # impressions / clicks based. An operator who hasn't
        # configured a Metrika goal still gets the low-CTR signal.
        low_ctr_no_goal = _perf(
            campaign_id=51,
            name="non-brand",
            clicks=50,
            cost_rub=500.0,
            impressions=50_000,
            conversions=0,  # no goal_id → no conversions data
            cpa_rub=None,
            cr_pct=None,
        )
        _patch_reporting(monkeypatch, _FakeReportingService(overview=[low_ctr_no_goal]))

        async with HealthCheckService(settings) as svc:
            report = await svc.run_account_check(date_range=_WEEK, goal_id=None)

        low_ctr_findings = [f for f in report.findings if f.rule_id == "low_ctr"]
        assert len(low_ctr_findings) == 1

    async def test_threshold_boundary_at_min_impressions(
        self,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Boundary check: exactly MIN_IMPRESSIONS-1 must skip;
        # MIN_IMPRESSIONS itself must evaluate. Documented
        # explicitly because it's the kind of off-by-one a future
        # refactor (e.g. switching ``<`` to ``<=``) would break
        # without anyone noticing.
        from yadirect_agent.services.health_check import LowCtrRule

        below = _perf(
            campaign_id=1,
            clicks=0,
            cost_rub=10.0,
            impressions=LowCtrRule.MIN_IMPRESSIONS - 1,
        )
        at = _perf(
            campaign_id=2,
            clicks=0,
            cost_rub=10.0,
            impressions=LowCtrRule.MIN_IMPRESSIONS,
        )
        _patch_reporting(monkeypatch, _FakeReportingService(overview=[below, at]))

        async with HealthCheckService(settings) as svc:
            report = await svc.run_account_check(date_range=_WEEK, goal_id=100)

        low_ctr_findings = [f for f in report.findings if f.rule_id == "low_ctr"]
        # ``below`` skipped, ``at`` evaluated → only one finding
        # (for campaign_id=2 since 0 clicks / N impressions = 0%
        # CTR which is below threshold).
        assert len(low_ctr_findings) == 1
        assert low_ctr_findings[0].campaign_id == 2
