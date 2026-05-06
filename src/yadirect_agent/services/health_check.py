"""Account health check service (M15.5.1, M15.5.2-3).

Consumes data from two sources and applies a battery of rule-based
checks:

1. **Metrika perf rows** — ``CampaignPerformance`` from the M6
   ``ReportingService.account_overview``. Each ``_Rule`` subclass
   has a synchronous ``check(perf)`` method returning zero or one
   ``Finding``. Used for cost/conversion-based rules
   (BurningCampaign, HighCpa).
2. **Direct API state** — moderation status of ads / keywords.
   Each ``_DirectStateRule`` subclass has an async
   ``collect_findings(direct, *, campaigns)`` method returning
   any number of findings (typically aggregated per-campaign).
   Used for state-based rules (RejectedAds, RejectedKeywords).

Two parallel rule lists rather than one unified interface because
the data shapes differ enough that a common base class would
either require Optional fields everywhere or push tribal knowledge
of "which rules consume what" into every site that builds a rule
list. Two lists, two clear contracts.

Why rules-as-classes rather than a flat function:

- Each rule has its own thresholds (``MIN_BURN_RUB``,
  ``HIGH_CPA_MULTIPLIER``, etc.) that are easier to override per
  test or per future ``agent_policy.yml`` knob when they live as
  class-level constants.
- Adding a new rule (M15.5.4 CTR-drift, etc.) is "drop a class
  in the rules list" — no service-level changes.
- The rule_id documented on the class is the stable identifier
  used in audit logs, CLI filtering, and M12 reports.

This file deliberately does NOT consume ``Settings`` policy values
yet — the thresholds are class-level constants. M15.5 follow-up
work will plumb them through ``agent_policy.yml`` once the policy
schema gains health-check sections.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Self

from ..clients.direct import DirectService
from ..config import Settings
from ..models.campaigns import Campaign, CampaignState
from ..models.health import Finding, HealthReport, Severity
from ..models.keywords import Keyword
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

    # Strict-greater-than threshold: a campaign spending exactly
    # MIN_BURN_RUB with 0 conversions is treated as below-threshold
    # noise (the rule uses ``cost_rub <= MIN_BURN_RUB`` to skip).
    # 50 RUB over a 7-day window with 0 conversions is statistically
    # nothing on most accounts; calling it out wastes operator
    # attention. This becomes a Settings.policy knob in a follow-up.
    # (auditor M15.5.1 LOW-5: boundary semantics documented and
    # tested at exactly the threshold + one cent above.)
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


class LowCtrRule(_Rule):
    """Flag campaigns whose CTR is too low to be effective.

    CTR = clicks / impressions * 100%. A campaign that shows but
    rarely gets clicked is a creative-iteration signal: the ad
    text/headline isn't connecting with the auction's audience,
    or the keyword targeting is broader than the creative
    expects. WARNING severity (not HIGH) because the operator may
    have legitimate reasons (brand campaigns on broad terms,
    awareness plays); the rule surfaces the candidate, the
    operator decides.

    Pre-conditions:

    - ``perf.impressions`` must meet ``MIN_IMPRESSIONS`` (1000) so
      a near-empty campaign isn't flagged on noise. ``5 impressions
      with 0 clicks = 0% CTR`` is statistically meaningless; the
      next day's data could shift to 60%.
    - ``perf.impressions > 0`` to avoid ZeroDivisionError on
      campaigns with no impression data at all (paused, not yet
      running, no Direct→Metrika linkage in Settings).
    - The computed CTR must be strictly below ``MIN_CTR_PCT``
      (0.5%). At-or-above is a pass.

    Unlike ``BurningCampaignRule`` and ``HighCpaRule``, this rule
    does NOT require ``goal_id`` — CTR is purely impressions /
    clicks; an operator who hasn't configured a Metrika goal still
    gets the low-CTR signal. This is the first rule of its kind;
    future creative-side rules (low CR, high bounce) will share
    the goal-independence.
    """

    rule_id = "low_ctr"

    # 0.5% is conservative — the typical Direct ad-network baseline
    # is 1-3% on broad keywords, 5%+ on tight branded keywords.
    # 0.5% catches the "nobody is clicking" case without flagging
    # legitimate brand/awareness campaigns on broad terms. Becomes
    # a Settings.policy knob in a follow-up.
    MIN_CTR_PCT: float = 0.5

    # Statistical-significance gate. 1000 impressions over a 7-day
    # window gives the operator something concrete to act on; below
    # this, the CTR sample is too noisy. (Operator-side: a fresh
    # campaign with <1000 impressions in a week is itself a signal,
    # but a different one — "this campaign isn't getting served"
    # belongs to a future "lost-impression-share" rule, not here.)
    MIN_IMPRESSIONS: int = 1000

    def check(
        self,
        perf: CampaignPerformance,
        *,
        goal_id: int | None,
    ) -> Finding | None:
        # Goal-independent: CTR doesn't need conversions.
        if perf.impressions < self.MIN_IMPRESSIONS:
            return None
        # Defensive divisor guard. ``MIN_IMPRESSIONS >= 1`` covers
        # the math, but the explicit check makes the intent
        # readable for future maintainers and survives a regression
        # that lowered the threshold to 0.
        if perf.impressions == 0:
            return None
        ctr_pct = perf.clicks / perf.impressions * 100.0
        if ctr_pct >= self.MIN_CTR_PCT:
            return None

        message = (
            f"campaign '{perf.campaign_name}' has low CTR "
            f"{ctr_pct:.2f}% ({perf.clicks} clicks / {perf.impressions} "
            f"impressions over {perf.date_range.start.isoformat()} to "
            f"{perf.date_range.end.isoformat()})"
        )
        return Finding(
            rule_id=self.rule_id,
            severity=Severity.WARNING,
            campaign_id=perf.campaign_id,
            campaign_name=perf.campaign_name,
            message=message,
            estimated_impact_rub=None,
        )


class _DirectStateRule:
    """Base for rules that consume Direct API state (not Metrika perf).

    The two implementations (``RejectedAdsRule``, ``RejectedKeywordsRule``)
    differ from ``_Rule`` subclasses in three ways:

    1. **Async** — they walk Direct API endpoints (``adgroups.get`` →
       ``ads.get`` / ``keywords.get``); a sync interface would force
       ``HealthCheckService`` to block its event loop.
    2. **Account-scoped, not per-row** — the input is the full list
       of active campaigns, not a single ``CampaignPerformance``;
       rules typically aggregate findings per-campaign rather than
       returning one finding per scanned entity.
    3. **Multiple findings per call** — the return type is
       ``list[Finding]``, not ``Finding | None``, because one
       campaign can have many rejected entities and we still want
       to emit a (single, aggregated) finding for it.

    The ``campaigns`` argument is pre-filtered to active (non-archived)
    campaigns by ``HealthCheckService.run_account_check`` — rules
    don't have to repeat the filter.
    """

    rule_id: str = ""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def collect_findings(
        self,
        direct: DirectService,
        *,
        campaigns: list[Campaign],
    ) -> list[Finding]:
        raise NotImplementedError


class RejectedAdsRule(_DirectStateRule):
    """Flag campaigns containing ads rejected by moderation.

    Aggregates per-campaign: one ``Finding`` per campaign with N
    rejected ads, not N findings. Rationale: the operator's mental
    model is "campaign X has issues", not "ad 5001 has an issue,
    also ad 5002 has an issue". One actionable line per campaign
    keeps the CLI table readable on accounts where one moderation
    event rejects 50+ creatives at once.
    """

    rule_id = "rejected_ads"

    # How many ad titles to quote inline before truncating with
    # "+N more". Three is enough for the operator to recognise the
    # affected campaign theme without overflowing a terminal line.
    SAMPLE_LIMIT: int = 3

    async def collect_findings(
        self,
        direct: DirectService,
        *,
        campaigns: list[Campaign],
    ) -> list[Finding]:
        if not campaigns:
            return []
        campaign_ids = [c.id for c in campaigns]
        rejected = await direct.scan_rejected_ads(campaign_ids=campaign_ids)
        if not rejected:
            return []

        name_by_id = {c.id: c.name for c in campaigns}
        grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for ad in rejected:
            cid = ad.get("CampaignId")
            # Skip orphan rows (CampaignId mismatch with our
            # active-campaigns set) — defensive against API drift
            # where the scan returns campaigns we didn't ask for.
            if isinstance(cid, int) and cid in name_by_id:
                grouped[cid].append(ad)

        findings: list[Finding] = []
        for cid, ads in grouped.items():
            campaign_name = name_by_id[cid]
            sample = []
            for ad in ads[: self.SAMPLE_LIMIT]:
                title = (ad.get("TextAd") or {}).get("Title")
                if title:
                    sample.append(f"'{title}'")
                else:
                    # Some ad types (image, dynamic) don't have a
                    # ``TextAd.Title``; fall back to the bare ID
                    # so the operator still has a handle.
                    sample.append(f"ad_id={ad.get('Id')}")
            extra_count = len(ads) - self.SAMPLE_LIMIT
            extra = f" (+{extra_count} more)" if extra_count > 0 else ""
            message = (
                f"{len(ads)} ad(s) rejected by moderation in campaign "
                f"'{campaign_name}': {', '.join(sample)}{extra}"
            )
            findings.append(
                Finding(
                    rule_id=self.rule_id,
                    severity=Severity.HIGH,
                    campaign_id=cid,
                    campaign_name=campaign_name,
                    message=message,
                    estimated_impact_rub=None,
                )
            )
        return findings


class RejectedKeywordsRule(_DirectStateRule):
    """Flag campaigns containing keywords rejected by moderation.

    Symmetric to ``RejectedAdsRule``: aggregated per-campaign,
    HIGH severity, sample-limited message.
    """

    rule_id = "rejected_keywords"

    SAMPLE_LIMIT: int = 3

    async def collect_findings(
        self,
        direct: DirectService,
        *,
        campaigns: list[Campaign],
    ) -> list[Finding]:
        if not campaigns:
            return []
        campaign_ids = [c.id for c in campaigns]
        rejected = await direct.scan_rejected_keywords(campaign_ids=campaign_ids)
        if not rejected:
            return []

        name_by_id = {c.id: c.name for c in campaigns}
        grouped: dict[int, list[Keyword]] = defaultdict(list)
        for kw in rejected:
            if kw.campaign_id is not None and kw.campaign_id in name_by_id:
                grouped[kw.campaign_id].append(kw)

        findings: list[Finding] = []
        for cid, kws in grouped.items():
            campaign_name = name_by_id[cid]
            sample = [f"'{kw.keyword}'" for kw in kws[: self.SAMPLE_LIMIT]]
            extra_count = len(kws) - self.SAMPLE_LIMIT
            extra = f" (+{extra_count} more)" if extra_count > 0 else ""
            message = (
                f"{len(kws)} keyword(s) rejected by moderation in campaign "
                f"'{campaign_name}': {', '.join(sample)}{extra}"
            )
            findings.append(
                Finding(
                    rule_id=self.rule_id,
                    severity=Severity.HIGH,
                    campaign_id=cid,
                    campaign_name=campaign_name,
                    message=message,
                    estimated_impact_rub=None,
                )
            )
        return findings


class HealthCheckService:
    """Run a battery of rules over Metrika perf + Direct state."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        # Order matters for the operator's reading: HIGH-severity rules
        # first so the most-actionable findings cluster at the top of
        # the per-campaign output before the CLI re-sorts globally.
        self._rules: list[_Rule] = [
            BurningCampaignRule(settings),
            HighCpaRule(settings),
            LowCtrRule(settings),
        ]
        self._direct_rules: list[_DirectStateRule] = [
            RejectedAdsRule(settings),
            RejectedKeywordsRule(settings),
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
        """Run all rules over the account overview + Direct state.

        ``goal_id`` is optional but most perf-rules are pre-conditioned
        on it — without conversions data, the conclusions degrade to
        cost-only signals that produce false positives. Direct-state
        rules don't need it (rejection status is goal-independent).
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

        if self._direct_rules:
            async with DirectService(self._settings) as direct:
                campaigns = await direct.get_campaigns()
                # Active = anything not archived. Archived campaigns
                # don't burn budget; their rejected entities aren't
                # actionable, so excluding them up-front is both a
                # signal-quality fix AND a bandwidth saver (one less
                # adgroup-walk per archived campaign).
                active = [c for c in campaigns if c.state != CampaignState.ARCHIVED]
                for direct_rule in self._direct_rules:
                    rule_findings = await direct_rule.collect_findings(direct, campaigns=active)
                    findings.extend(rule_findings)

        return HealthReport(date_range=date_range, findings=findings)


# Re-export so monkeypatch in tests targets a stable name.
__all__ = [
    "BurningCampaignRule",
    "DirectService",
    "HealthCheckService",
    "HighCpaRule",
    "LowCtrRule",
    "RejectedAdsRule",
    "RejectedKeywordsRule",
    "ReportingService",
]
