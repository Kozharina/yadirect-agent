"""Account health check service (M15.5.1, M15.5.2-3, M15.5.4, M15.5.5).

Consumes data from three sources and applies a battery of rule-based
checks:

1. **Metrika perf rows** — ``CampaignPerformance`` from the M6
   ``ReportingService.account_overview``. Each ``_Rule`` subclass
   has a synchronous ``check(perf)`` method returning zero or one
   ``Finding``. Used for cost/conversion-based rules
   (BurningCampaign, HighCpa, LowCtr).
2. **Direct API state** — moderation status of ads / keywords.
   Each ``_DirectStateRule`` subclass has an async
   ``collect_findings(direct, *, campaigns)`` method returning
   any number of findings (typically aggregated per-campaign).
   Used for state-based rules (RejectedAds, RejectedKeywords).
3. **Historical Metrika perf** — last week's ``HealthSnapshot``
   from ``HealthHistoryStore``, paired with this week's perf.
   Each ``_HistoricalRule`` subclass has a synchronous
   ``check(perf, *, previous_snapshot)`` method. Used for
   week-over-week rules (CtrDrift). Historical rules silently
   skip when no ``history_store`` is wired in — the M15.5.x
   ``--no-llm`` mode keeps working on fresh installs.

Three parallel rule lists rather than one unified interface
because the data shapes differ enough that a common base class
would either require Optional fields everywhere or push tribal
knowledge of "which rules consume what" into every site that
builds a rule list. Three lists, three clear contracts.

Why rules-as-classes rather than a flat function:

- Each rule has its own thresholds (``MIN_BURN_RUB``,
  ``HIGH_CPA_MULTIPLIER``, etc.) that are easier to override per
  test or per future ``agent_policy.yml`` knob when they live as
  class-level constants.
- Adding a new rule is "drop a class in the matching rules list"
  — no service-level changes.
- The rule_id documented on the class is the stable identifier
  used in audit logs, CLI filtering, and M12 reports.

This file deliberately does NOT consume ``Settings`` policy values
yet — the thresholds are class-level constants. M15.5 follow-up
work will plumb them through ``agent_policy.yml`` once the policy
schema gains health-check sections.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from typing import Any, Self

from ..clients.direct import DirectService
from ..config import Settings
from ..models.campaigns import Campaign, CampaignState
from ..models.health import Finding, HealthReport, Severity
from ..models.health_history import HealthSnapshot
from ..models.keywords import Keyword
from ..models.metrika import CampaignPerformance, DateRange
from .health_history_store import HealthHistoryStore
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


class _HistoricalRule:
    """Base for rules that compare current perf against a previous snapshot.

    Distinct from ``_Rule`` because the input shape is different
    (``CampaignPerformance`` for now + ``HealthSnapshot | None``
    for the historical baseline) and the lifecycle is different
    (the service must load history before invoking and save
    snapshots after — neither concern belongs in ``_Rule``).

    Stateless; safe to instantiate once. Receives ``settings`` for
    parity with ``_Rule`` even though the only current implementation
    (``CtrDriftRule``) uses class-level constants.
    """

    rule_id: str = ""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def check(
        self,
        perf: CampaignPerformance,
        *,
        previous_snapshot: HealthSnapshot | None,
    ) -> Finding | None:
        raise NotImplementedError


class CtrDriftRule(_HistoricalRule):
    """Flag campaigns whose CTR dropped sharply week-over-week.

    Drift = relative drop, not absolute change:
    ``(prev_ctr - current_ctr) / prev_ctr * 100``. A 2.0% → 1.4%
    week is a 30% drop; same as 1.0% → 0.7%. Operators think in
    relative terms ("CTR fell by a third"), so the rule speaks
    that language.

    Pre-conditions (all required, all silent skips):

    - ``previous_snapshot`` must not be None — first-ever run for
      a campaign has nothing to compare against. The snapshot
      gets persisted by the service for the next run.
    - Both windows must meet ``MIN_IMPRESSIONS`` (1000). A
      previous-week sample of 200 impressions is statistical
      noise; comparing against it produces false positives. Same
      asymmetry guard for the current week (campaign paused
      mid-week → low impressions → would falsely fire).
    - ``previous_snapshot.ctr_pct`` must not be None. None means
      previous week had impressions=0; we cannot compute a drop
      from undefined.
    - The relative drop must EXCEED ``MAX_CTR_DROP_PCT`` (30.0).
      At-or-below the threshold is a pass (operator already sees
      smaller drift in the standard week-over-week reports).

    Severity: WARNING. Not HIGH because legitimate week-over-week
    drift happens (Black Friday week vs the week after, brand
    campaign ramp-down). The operator decides whether to act.

    Why drop-only, not a symmetric absolute change rule: CTR
    going UP is good news. A symmetric rule would surface every
    optimisation win as an "alert" and train the operator to
    ignore the channel. Asymmetry is deliberate.
    """

    rule_id = "ctr_drift"

    # 30% relative drop is the threshold media-buyers use as a
    # "this needs attention" gate in their weekly reviews. Smaller
    # drops are noise on most accounts; larger drops signal
    # creative fatigue, ad-rotation issue, or competitive pressure.
    # Becomes a Settings.policy knob in a follow-up.
    MAX_CTR_DROP_PCT: float = 30.0

    # Same statistical-significance gate as ``LowCtrRule`` —
    # consistent threshold across CTR-related rules makes the
    # operator's mental model simple ("CTR rules need 1000+
    # impressions").
    MIN_IMPRESSIONS: int = 1000

    def check(
        self,
        perf: CampaignPerformance,
        *,
        previous_snapshot: HealthSnapshot | None,
    ) -> Finding | None:
        if previous_snapshot is None:
            return None
        if previous_snapshot.ctr_pct is None:
            return None
        if previous_snapshot.impressions < self.MIN_IMPRESSIONS:
            return None
        if perf.impressions < self.MIN_IMPRESSIONS:
            return None
        # Defensive divisor guard — the MIN_IMPRESSIONS gate above
        # implies impressions > 0, but a future regression that
        # lowered the threshold to 0 must not silently divide by
        # zero. Same shape as LowCtrRule.
        if perf.impressions == 0:
            return None

        previous_ctr = previous_snapshot.ctr_pct
        # previous_ctr is float (None caught above); divisor guard
        # via MIN_IMPRESSIONS implies it's strictly > 0 in any
        # realistic scenario — but pin explicitly so a synthetic
        # test pinning ctr_pct=0.0 doesn't ZeroDivisionError us.
        if previous_ctr <= 0.0:
            return None

        current_ctr = perf.clicks / perf.impressions * 100.0
        if current_ctr >= previous_ctr:
            # CTR went up or stayed the same — not drift, not an alert.
            return None

        drop_pct = (previous_ctr - current_ctr) / previous_ctr * 100.0
        if drop_pct <= self.MAX_CTR_DROP_PCT:
            return None

        message = (
            f"campaign '{perf.campaign_name}' CTR dropped "
            f"{drop_pct:.0f}% week-over-week: "
            f"{previous_ctr:.1f}% → {current_ctr:.1f}% "
            f"({perf.clicks} clicks / {perf.impressions} impressions "
            f"this week vs {previous_snapshot.clicks} / "
            f"{previous_snapshot.impressions} previously)"
        )
        return Finding(
            rule_id=self.rule_id,
            severity=Severity.WARNING,
            campaign_id=perf.campaign_id,
            campaign_name=perf.campaign_name,
            message=message,
            # estimated_impact_rub=None because lost-clicks-equivalent
            # in RUB requires CPC, which we don't carry on the perf
            # row (cost_rub is window-aggregated, not per-click).
            # A future enhancement could derive avg-CPC from
            # cost_rub / clicks of the previous week and multiply
            # by the lost click delta — out of scope for slice 1.
            estimated_impact_rub=None,
        )


def _perf_to_snapshot(
    perf: CampaignPerformance,
    *,
    snapshot_at: datetime,
) -> HealthSnapshot:
    """Convert a current-week ``CampaignPerformance`` into a snapshot
    suitable for ``HealthHistoryStore.append``.

    CTR computed here (not on the perf row) because perf rows
    don't carry CTR — it's the rule's domain knowledge. ``None``
    when impressions == 0, matching the documented contract.
    """
    ctr_pct = (perf.clicks / perf.impressions * 100.0) if perf.impressions > 0 else None
    return HealthSnapshot(
        snapshot_at=snapshot_at,
        date_range=perf.date_range,
        campaign_id=perf.campaign_id,
        clicks=perf.clicks,
        impressions=perf.impressions,
        ctr_pct=ctr_pct,
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
    """Run a battery of rules over Metrika perf + Direct state + history."""

    def __init__(
        self,
        settings: Settings,
        *,
        history_store: HealthHistoryStore | None = None,
    ) -> None:
        self._settings = settings
        self._history_store = history_store
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
        self._historical_rules: list[_HistoricalRule] = [
            CtrDriftRule(settings),
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
        """Run all rules over the account overview + Direct state + history.

        ``goal_id`` is optional but most perf-rules are pre-conditioned
        on it — without conversions data, the conclusions degrade to
        cost-only signals that produce false positives. Direct-state
        rules and the historical rules don't need it (rejection
        status is goal-independent; CTR is purely
        impressions / clicks).

        Lifecycle when ``history_store`` is wired in:
        1. Load latest-per-campaign snapshots BEFORE running any rules.
        2. Run perf-rules + direct-state-rules (existing behaviour).
        3. Run historical rules with each campaign's previous snapshot
           injected (or None if first-ever sighting).
        4. Append current overview as new snapshots so the next run
           has a baseline.

        With ``history_store=None`` (default), step 1, 3, and 4 are
        no-ops — preserves the M15.5.x ``--no-llm`` mode contract for
        callers that haven't been updated to wire in history yet.
        """
        previous_by_campaign: dict[int, HealthSnapshot] = {}
        if self._history_store is not None:
            previous_by_campaign = self._history_store.load_latest_per_campaign()

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

            if self._history_store is not None and self._historical_rules:
                previous = previous_by_campaign.get(perf.campaign_id)
                for historical_rule in self._historical_rules:
                    historical_finding = historical_rule.check(
                        perf,
                        previous_snapshot=previous,
                    )
                    if historical_finding is not None:
                        findings.append(historical_finding)

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

        # Persist current overview as snapshots for the next run.
        # Done AFTER rule execution so a rule that crashes mid-run
        # doesn't pollute the history with a half-evaluated week.
        # (A real crash propagates here and the snapshot save is
        # skipped — the next run will then see the older baseline,
        # which is correct: we never crashed past the rule, so we
        # never observed this week.)
        if self._history_store is not None and overview:
            now = datetime.now(UTC)
            snapshots = [_perf_to_snapshot(perf, snapshot_at=now) for perf in overview]
            self._history_store.append(snapshots)

        return HealthReport(date_range=date_range, findings=findings)


# Re-export so monkeypatch in tests targets a stable name.
__all__ = [
    "BurningCampaignRule",
    "CtrDriftRule",
    "DirectService",
    "HealthCheckService",
    "HealthHistoryStore",
    "HighCpaRule",
    "LowCtrRule",
    "RejectedAdsRule",
    "RejectedKeywordsRule",
    "ReportingService",
]
