"""Tests for health DTOs (M15.5.1).

Trust the dataclass machinery; pin the *invariants* we own:

- ``Severity`` enum has the expected three levels.
- ``Finding`` is frozen.
- ``HealthReport.has_findings`` and ``findings_by_severity`` work.
- ``default_window`` ends yesterday and covers the requested days.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import date, timedelta

import pytest

from yadirect_agent.models.health import (
    Finding,
    HealthReport,
    Severity,
    default_window,
)
from yadirect_agent.models.metrika import DateRange


class TestSeverity:
    def test_known_levels(self) -> None:
        assert Severity.INFO == "info"
        assert Severity.WARNING == "warning"
        assert Severity.HIGH == "high"


class TestFinding:
    def _make(self, **overrides: object) -> Finding:
        defaults: dict[str, object] = {
            "rule_id": "burning_campaign",
            "severity": Severity.HIGH,
            "campaign_id": 42,
            "campaign_name": "brand",
            "message": "campaign 'brand' burned 2400 RUB with 0 conversions",
            "estimated_impact_rub": 2400.0,
        }
        defaults.update(overrides)
        return Finding(**defaults)  # type: ignore[arg-type]

    def test_construction(self) -> None:
        f = self._make()

        assert f.rule_id == "burning_campaign"
        assert f.severity == Severity.HIGH
        assert f.campaign_id == 42
        assert f.estimated_impact_rub == pytest.approx(2400.0)

    def test_frozen_blocks_mutation(self) -> None:
        f = self._make()

        with pytest.raises(FrozenInstanceError):
            f.severity = Severity.INFO  # type: ignore[misc]

    def test_account_level_finding_allows_no_campaign(self) -> None:
        # Some future rules (e.g., billing-state checks) target the
        # account, not a campaign. campaign_id and campaign_name
        # are both Optional.
        f = self._make(campaign_id=None, campaign_name=None)

        assert f.campaign_id is None
        assert f.campaign_name is None

    def test_estimated_impact_optional(self) -> None:
        # "We see a problem but can't quantify cheaply" is a valid
        # finding shape — None, not 0.
        f = self._make(estimated_impact_rub=None)

        assert f.estimated_impact_rub is None


class TestHealthReport:
    def _make_report(self, findings: list[Finding] | None = None) -> HealthReport:
        return HealthReport(
            date_range=DateRange(start=date(2026, 4, 1), end=date(2026, 4, 7)),
            findings=findings or [],
        )

    def test_empty_report_has_no_findings(self) -> None:
        r = self._make_report()

        assert r.has_findings is False
        assert r.findings == []

    def test_with_findings(self) -> None:
        f1 = Finding(
            rule_id="burning_campaign",
            severity=Severity.HIGH,
            campaign_id=42,
            campaign_name="brand",
            message="…",
        )
        f2 = Finding(
            rule_id="high_cpa",
            severity=Severity.WARNING,
            campaign_id=51,
            campaign_name="non-brand",
            message="…",
        )
        r = self._make_report(findings=[f1, f2])

        assert r.has_findings is True
        assert len(r.findings) == 2

    def test_findings_by_severity(self) -> None:
        f_high = Finding(
            rule_id="burning",
            severity=Severity.HIGH,
            campaign_id=42,
            campaign_name="x",
            message="…",
        )
        f_warn1 = Finding(
            rule_id="high_cpa",
            severity=Severity.WARNING,
            campaign_id=51,
            campaign_name="y",
            message="…",
        )
        f_warn2 = Finding(
            rule_id="high_cpa",
            severity=Severity.WARNING,
            campaign_id=52,
            campaign_name="z",
            message="…",
        )
        r = self._make_report(findings=[f_high, f_warn1, f_warn2])

        high = r.findings_by_severity(Severity.HIGH)
        warn = r.findings_by_severity(Severity.WARNING)
        info = r.findings_by_severity(Severity.INFO)

        assert len(high) == 1
        assert high[0] is f_high
        assert len(warn) == 2
        assert info == []


class TestDefaultWindow:
    def test_seven_days_default_ends_yesterday(self) -> None:
        r = default_window()

        today = date.today()
        assert r.end == today - timedelta(days=1)
        assert r.start == today - timedelta(days=7)

    def test_custom_days(self) -> None:
        r = default_window(days=30)

        today = date.today()
        assert r.end == today - timedelta(days=1)
        assert r.start == today - timedelta(days=30)

    def test_one_day_window_is_yesterday_only(self) -> None:
        r = default_window(days=1)

        today = date.today()
        assert r.start == r.end == today - timedelta(days=1)
