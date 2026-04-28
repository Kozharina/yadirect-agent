"""Account health check service (M15.5.1).

Consumes ``CampaignPerformance`` rows from the M6
``ReportingService.account_overview`` and applies a battery of
rule-based checks. Each rule is an independent class with a
``check(perf)`` method returning zero or one ``Finding``; the
service runs them all and aggregates into a ``HealthReport``.

Why rules-as-classes rather than a flat function:

- Each rule has its own thresholds (``MIN_BURN_RUB``,
  ``HIGH_CPA_MULTIPLIER``, etc.) that are easier to override per
  test or per future ``agent_policy.yml`` knob when they live as
  class-level constants.
- Adding a new rule (M15.5.2 low-CTR, M15.5.3 query-drift, etc.)
  is "drop a class in the rules list" — no service-level changes.
- The rule_id documented on the class is the stable identifier
  used in audit logs, CLI filtering, and M12 reports.

This file deliberately does NOT consume ``Settings`` policy values
yet — the thresholds are class-level constants. M15.5 follow-up
work will plumb them through ``agent_policy.yml`` once the policy
schema gains health-check sections.
"""

from __future__ import annotations

from typing import Self

from ..config import Settings
from ..models.health import Finding, HealthReport, Severity
from ..models.metrika import CampaignPerformance, DateRange
from .reporting import ReportingService


class _Rule:
    """Base interface for a single rule. Stateless; safe to instantiate once.

    Rules receive the global ``settings`` so they can read account-wide
    knobs (target CPA, etc.) without HealthCheckService having to pass
    them through every call.
    """

    rule_id: str = ""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def check(
        self,
        perf: CampaignPerformance,
        *,
        goal_id: int | None,
    ) -> Finding | None:
        raise NotImplementedError


class BurningCampaignRule(_Rule):
    """Flag a campaign that's spending without converting.

    The most operator-visible signal in the rule-based mode. The
    pre-conditions are intentionally narrow to keep the false-positive
    rate low:

    - ``goal_id`` must be set — without conversions data, the rule
      cannot meaningfully fire (``conversions`` would always be 0
      by construction in account_overview).
    - ``cost_rub`` must exceed ``MIN_BURN_RUB`` so paused / micro-spend
      campaigns don't pollute the operator attention.
    - ``conversions`` must be exactly 0 — a single conversion at a
      poor CPA is a job for the high-CPA rule, not this one.
    """

    rule_id = "burning_campaign"

    # Below this threshold we treat zero-conversion spend as noise.
    # 50 RUB over a 7-day window with 0 conversions is statistically
    # nothing on most accounts; calling it out wastes operator
    # attention. This becomes a Settings.policy knob in a follow-up.
    MIN_BURN_RUB: float = 50.0

    def check(
        self,
        perf: CampaignPerformance,
        *,
        goal_id: int | None,
    ) -> Finding | None:
        if goal_id is None:
            return None
        if perf.cost_rub <= self.MIN_BURN_RUB:
            return None
        if perf.conversions != 0:
            return None

        message = (
            f"campaign '{perf.campaign_name}' burned "
            f"{perf.cost_rub:.0f} RUB with 0 conversions over "
            f"{perf.date_range.start.isoformat()} to {perf.date_range.end.isoformat()}"
        )
        return Finding(
            rule_id=self.rule_id,
            severity=Severity.HIGH,
            campaign_id=perf.campaign_id,
            campaign_name=perf.campaign_name,
            message=message,
            estimated_impact_rub=perf.cost_rub,
        )


class HighCpaRule(_Rule):
    """Flag campaigns whose CPA exceeds the operator's target.

    Distinct from BurningCampaignRule: this fires on campaigns that
    ARE converting, just expensively. WARNING severity, because the
    operator may have business reasons (brand campaigns often run
    higher CPA than performance campaigns; new product launches
    often run high while learning).

    Pre-conditions are tighter than the burning rule:

    - ``goal_id`` must be set (no conversions data → CPA is None →
      cannot compare).
    - ``Settings.account_target_cpa_rub`` must be set. None means
      "operator hasn't told us what good looks like"; firing on
      every campaign because we don't know the target is pure
      noise — silently skip.
    - ``perf.cpa_rub`` must NOT be None. Per the M6 contract,
      None means "undefined" (zero conversions or zero cost),
      never "infinity". A regression treating None as infinity
      would silently nuke every burning campaign as high-CPA
      instead of letting BurningCampaignRule emit the right
      HIGH finding.
    - ``perf.conversions`` must meet ``MIN_CONVERSIONS`` so a
      single conversion at "high CPA" doesn't trip the rule —
      it might just be variance, not a sustained issue.
    - ``perf.cpa_rub`` must exceed the target.
    """

    rule_id = "high_cpa"

    # Statistical-significance gate. With <5 conversions in the
    # window, "CPA = 1200 RUB" is a single data point or two —
    # the operator can't act on it without more evidence. This
    # threshold becomes a Settings.policy knob in a follow-up.
    MIN_CONVERSIONS: int = 5

    def check(
        self,
        perf: CampaignPerformance,
        *,
        goal_id: int | None,
    ) -> Finding | None:
        if goal_id is None:
            return None
        target = self._settings.account_target_cpa_rub
        if target is None:
            return None
        if perf.cpa_rub is None:
            return None
        if perf.conversions < self.MIN_CONVERSIONS:
            return None
        if perf.cpa_rub <= target:
            return None

        excess_per_conversion = perf.cpa_rub - target
        estimated_impact = excess_per_conversion * perf.conversions

        message = (
            f"campaign '{perf.campaign_name}' CPA "
            f"{perf.cpa_rub:.0f} RUB is above target "
            f"{target:.0f} RUB ({perf.conversions} conversions, "
            f"~{estimated_impact:.0f} RUB excess spend)"
        )
        return Finding(
            rule_id=self.rule_id,
            severity=Severity.WARNING,
            campaign_id=perf.campaign_id,
            campaign_name=perf.campaign_name,
            message=message,
            estimated_impact_rub=estimated_impact,
        )


class HealthCheckService:
    """Run a battery of rules over the M6 account_overview output."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        # Order matters for the operator's reading: HIGH-severity rules
        # first so the most-actionable findings cluster at the top of
        # the per-campaign output before the CLI re-sorts globally.
        self._rules: list[_Rule] = [
            BurningCampaignRule(settings),
            HighCpaRule(settings),
        ]

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    async def run_account_check(
        self,
        *,
        date_range: DateRange,
        goal_id: int | None = None,
    ) -> HealthReport:
        """Run all rules over the account overview.

        ``goal_id`` is optional but most rules are pre-conditioned on
        it — without conversions data, the conclusions degrade to
        cost-only signals that produce false positives.
        """
        async with ReportingService(self._settings) as reporting:
            overview = await reporting.account_overview(
                date_range=date_range,
                goal_id=goal_id,
            )

        findings: list[Finding] = []
        for perf in overview:
            for rule in self._rules:
                finding = rule.check(perf, goal_id=goal_id)
                if finding is not None:
                    findings.append(finding)

        return HealthReport(date_range=date_range, findings=findings)


# Re-export so monkeypatch in tests targets a stable name.
__all__ = [
    "BurningCampaignRule",
    "HealthCheckService",
    "HighCpaRule",
    "ReportingService",
]
