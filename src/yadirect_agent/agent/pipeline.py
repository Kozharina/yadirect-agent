"""Safety pipeline — orchestrates the 7 kill-switches into a single decision.

This is the integration point for M2.2 ``plan → confirm → execute``.
Callers (primarily the M2.2 ``@requires_plan`` decorator, landing in the
next PR) build an ``OperationPlan`` and hand it to
``SafetyPipeline.review(plan, context)`` along with a ``ReviewContext``
that carries the snapshots each check needs. The pipeline returns a
``SafetyDecision`` — ``allow``, ``confirm``, or ``reject`` — with the
list of blocking checks and warnings for the audit sink.

Responsibilities closed here, from the M2.0/M2.1 auditor backlog:

1. **Forbidden operations normalisation at call site.** Policy stores
   forbidden names already lowercased; the pipeline normalises
   ``plan.action`` the same way before comparing. An operator typo in
   the policy file and an agent call with subtly-different casing
   cannot both slip past a case-sensitive lookup.

2. **rollout_stage enforcement.** The field is no longer stored-but-
   ignored: each stage maps to an allowed-action set, and the pipeline
   rejects anything outside that set. Shadow = read-only. Assist =
   pause + negatives + bid ±10%. Autonomy_light = bid ±25%,
   budget ±15%, keyword creation. Autonomy_full = everything except
   ``forbidden_operations``.

3. **Global-gatekeeper vs per-op dispatching.** KS#6 (conversion
   integrity) and KS#7 (query drift) run first. If either is blocked,
   the entire plan is rejected regardless of per-op content — same
   plan, different semantics, same ``reject`` outcome. Per-op checks
   (KS#1-#5) run only if the gatekeepers pass.

4. **Cross-call TOCTOU for bids (KS#4).** ``SessionState`` tracks the
   maximum bid the pipeline has already approved per keyword in the
   current session. A follow-up plan proposing a strictly higher bid
   for the same keyword is blocked even if each individual
   ``check()`` call would pass its own snapshot-based inspection.

5. **Baseline provenance.** The pipeline is the sole constructor of
   ``ReviewContext``; callers don't hand the check a baseline they
   built themselves. Every baseline has a timestamp in the context,
   which the audit sink (M2.3) will surface so stale-baseline blind
   spots become visible.

Responsibilities explicitly deferred:

- Real service integration (``@requires_plan`` decorator and
  ``CampaignService.set_daily_budget`` wiring land in the next PR).
- ``apply-plan`` executor + status transitions in the JSONL store.
- Audit sink with JSONL + timestamps (M2.3).
- Daily-budget hard guard (M2.4 — a thin wrapper around KS#1 that
  reads ``AGENT_MAX_DAILY_BUDGET_RUB`` from env).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from .plans import OperationPlan
from .safety import (
    AccountBidSnapshot,
    AccountBudgetSnapshot,
    BudgetBalanceDriftCheck,
    BudgetCapCheck,
    BudgetChange,
    CheckResult,
    ConversionIntegrityCheck,
    ConversionsSnapshot,
    MaxCpcCheck,
    NegativeKeywordFloorCheck,
    Policy,
    ProposedBidChange,
    QualityScoreGuardCheck,
    QueryDriftCheck,
    RolloutStage,
    SearchQueriesSnapshot,
)

DecisionStatus = Literal["allow", "confirm", "reject"]


# --------------------------------------------------------------------------
# Actions x rollout stage.
#
# Each stage's allowed-action set is the cumulative union of the less
# permissive stages plus its own new permissions. A plan whose action
# string is not in the set is rejected before any check runs, so a
# malformed or unexpected action name can't slip through.
# --------------------------------------------------------------------------


# Read-only actions — always allowed, even in shadow.
_READ_ONLY_ACTIONS: frozenset[str] = frozenset(
    {
        "list_campaigns",
        "get_keywords",
        "get_ad_groups",
        "get_ads",
        "get_reports",
        "validate_phrases",
    }
)

# Stage "assist" adds to shadow.
_ASSIST_ACTIONS: frozenset[str] = _READ_ONLY_ACTIONS | frozenset(
    {
        "pause_campaigns",
        "add_negative_keywords",
        "set_keyword_bids",  # bounded to ±10% by policy.max_bid_increase_pct
    }
)

# Stage "autonomy_light" adds keyword creation + budget edits + resume.
_AUTONOMY_LIGHT_ACTIONS: frozenset[str] = _ASSIST_ACTIONS | frozenset(
    {
        "resume_campaigns",
        "set_campaign_budget",
        "add_keywords",
        "set_keyword_state",
    }
)

# Stage "autonomy_full" is everything we know how to express,
# minus whatever ``forbidden_operations`` explicitly blocks.
_AUTONOMY_FULL_ACTIONS: frozenset[str] = _AUTONOMY_LIGHT_ACTIONS | frozenset(
    {
        "create_campaign",
        "create_ad_group",
        "create_ad",
        "archive_campaigns",
    }
)

_STAGE_ALLOWED: dict[RolloutStage, frozenset[str]] = {
    "shadow": _READ_ONLY_ACTIONS,
    "assist": _ASSIST_ACTIONS,
    "autonomy_light": _AUTONOMY_LIGHT_ACTIONS,
    "autonomy_full": _AUTONOMY_FULL_ACTIONS,
}


def _is_mutating_action(action: str) -> bool:
    """Mutating = anything outside the read-only set."""
    return action not in _READ_ONLY_ACTIONS


# --------------------------------------------------------------------------
# Required snapshots per mutating action.
#
# Closes the auditor CRITICAL finding: an empty ``ReviewContext`` used to
# let every check be skipped and the plan auto-allowed. The pipeline
# now requires the appropriate snapshot to be present before a
# mutating action is considered, and rejects the plan otherwise.
#
# A "required" snapshot here is the one whose corresponding per-op
# check must run for the action to be considered checked. Temporal
# gatekeepers (KS#6/#7) remain optional at the pipeline level —
# they're cross-cutting; the next-PR decorator will wire them in.
# --------------------------------------------------------------------------


_BUDGET_SNAPSHOT_ACTIONS: frozenset[str] = frozenset(
    {
        "pause_campaigns",
        "resume_campaigns",
        "set_campaign_budget",
        "add_negative_keywords",
        "archive_campaigns",
        # NB: create_campaign is deliberately absent. A freshly-created
        # campaign has no spend-to-date to cap-check against; the budget
        # ceiling kicks in the moment the operator calls set_campaign_budget
        # (which *is* in this set). Structural creation falls through to
        # the gatekeepers + approval tier and defaults to confirm.
    }
)
_BID_SNAPSHOT_ACTIONS: frozenset[str] = frozenset(
    {
        "set_keyword_bids",
        "add_keywords",
        "set_keyword_state",
    }
)


def _required_snapshots(action: str) -> tuple[bool, bool]:
    """Return (needs_budget_snapshot, needs_bid_snapshot) for ``action``."""
    return (
        action in _BUDGET_SNAPSHOT_ACTIONS,
        action in _BID_SNAPSHOT_ACTIONS,
    )


# --------------------------------------------------------------------------
# Approval tiers — which mutating actions auto-run vs require confirm.
#
# The auditor HIGH finding: the original pipeline auto-allowed every
# mutating action that wasn't pause/resume/add_negative_keywords. That
# silently covers budget edits, keyword creation, campaign structure,
# and more — all of which should default to "human-in-the-loop" until
# an explicit policy knob says otherwise.
#
# Today the policy only exposes three auto-approve knobs (pause, resume,
# negative_keywords). Every other mutating action defaults to ``confirm``
# even when all kill-switches pass. When more fine-grained knobs land
# (auto_approve_budget_change, auto_approve_structural_change, etc.),
# this table grows.
# --------------------------------------------------------------------------


_AUTO_APPROVABLE_ACTIONS: frozenset[str] = frozenset(
    {
        "pause_campaigns",
        "resume_campaigns",
        "add_negative_keywords",
    }
)


# --------------------------------------------------------------------------
# Data surfaces the pipeline consumes / produces.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ReviewContext:
    """Everything a full pipeline review needs, built exclusively by the
    pipeline layer (never supplied by the agent directly).

    Any snapshot field may be ``None`` — the pipeline only runs the
    checks whose data is present. The decorator (next PR) reads each
    snapshot from its dedicated source and records the read timestamp
    in ``baseline_timestamp`` so the audit sink (M2.3) can flag stale
    baselines.

    Checks that receive ``None`` are *skipped*, not *failed* — missing
    data is a different outcome than "data present and below
    threshold". The pipeline's aggregate decision includes a
    ``skipped_checks`` list so M2.3 can surface these to operators.
    """

    # Per-op snapshots (KS#1 / KS#2).
    budget_snapshot: AccountBudgetSnapshot | None = None
    bid_snapshot: AccountBidSnapshot | None = None

    # Temporal baselines (KS#5 / KS#6 / KS#7).
    budget_baseline: AccountBudgetSnapshot | None = None
    conversions_baseline: ConversionsSnapshot | None = None
    conversions_current: ConversionsSnapshot | None = None
    queries_baseline: SearchQueriesSnapshot | None = None
    queries_current: SearchQueriesSnapshot | None = None

    # Plan-specific changes (derived by the decorator from the call).
    budget_changes: list[BudgetChange] = field(default_factory=list)
    bid_changes: list[ProposedBidChange] = field(default_factory=list)

    # Provenance: when were the baselines read?
    baseline_timestamp: datetime | None = None


@dataclass(frozen=True)
class SafetyDecision:
    """The pipeline's verdict on a plan.

    - ``allow``: every applicable check passed. Executor may proceed.
    - ``confirm``: blocking checks passed, but policy requires human
      approval (e.g. ``auto_approve_resume=False`` and plan is a
      resume). Executor writes the plan to ``pending_plans.jsonl``
      and returns without touching the API.
    - ``reject``: at least one check blocked or the action is
      forbidden / above the rollout stage. Executor raises without
      writing anything.

    ``blocking_checks`` carries the per-check results that drove the
    decision; ``warnings`` carries warn-level results that were
    surfaced but did not force a block. The audit sink (M2.3) writes
    both lists.
    """

    status: DecisionStatus
    reason: str
    blocking_checks: list[CheckResult] = field(default_factory=list)
    warnings: list[CheckResult] = field(default_factory=list)
    skipped_checks: list[str] = field(default_factory=list)

    @property
    def requires_confirmation(self) -> bool:
        return self.status == "confirm"

    @property
    def allowed(self) -> bool:
        return self.status == "allow"


@dataclass
class SessionState:
    """Mutable cross-call state the pipeline holds for the lifetime of one
    agent run.

    Primary use today: the KS#4 TOCTOU register. An agent that splits
    a bid increase into many small hops can pass every per-call
    snapshot-based inspection, but the pipeline remembers the maximum
    approved bid per keyword and refuses the second hop.
    """

    max_approved_bid_per_keyword: dict[int, float] = field(default_factory=dict)

    def record_approved_bid(self, keyword_id: int, bid_rub: float) -> None:
        prior = self.max_approved_bid_per_keyword.get(keyword_id, 0.0)
        if bid_rub > prior:
            self.max_approved_bid_per_keyword[keyword_id] = bid_rub

    def approved_bid_ceiling(self, keyword_id: int) -> float | None:
        return self.max_approved_bid_per_keyword.get(keyword_id)


# --------------------------------------------------------------------------
# Pipeline.
# --------------------------------------------------------------------------


class SafetyPipeline:
    """Aggregates the seven kill-switches into a single ``review`` call.

    Construction: ``SafetyPipeline(policy)``. The pipeline builds all
    seven check objects from the policy's slice-policies up front —
    they're cheap to construct (pydantic-validated, frozen), and
    building once at pipeline creation means the per-call overhead is
    only the check invocations themselves.

    A shared ``SessionState`` can be passed in to track cross-call
    context (e.g. the KS#4 TOCTOU register) for the duration of one
    agent run. If omitted, a fresh state is created.
    """

    def __init__(
        self,
        policy: Policy,
        *,
        session_state: SessionState | None = None,
    ) -> None:
        self._policy = policy
        self._session = session_state or SessionState()
        self._budget_cap = BudgetCapCheck(policy.budget_cap)
        self._max_cpc = MaxCpcCheck(policy.max_cpc)
        self._negative_floor = NegativeKeywordFloorCheck(policy.negative_keyword_floor)
        self._qs_guard = QualityScoreGuardCheck(policy.quality_score_guard)
        self._balance_drift = BudgetBalanceDriftCheck(policy.budget_balance_drift)
        self._conversion_integrity = ConversionIntegrityCheck(policy.conversion_integrity)
        self._query_drift = QueryDriftCheck(policy.query_drift)

    # ------------------------------------------------------------------

    @property
    def session(self) -> SessionState:
        return self._session

    @property
    def policy(self) -> Policy:
        return self._policy

    # ------------------------------------------------------------------

    def review(self, plan: OperationPlan, context: ReviewContext) -> SafetyDecision:
        """Return an allow / confirm / reject verdict on ``plan``."""
        # 0. Hard-forbidden actions — no check runs, no context needed.
        normalised_action = plan.action.strip().lower()
        if normalised_action in self._policy.forbidden_operations:
            return SafetyDecision(
                status="reject",
                reason=(f"action {normalised_action!r} is in policy.forbidden_operations"),
            )

        # 1. Rollout stage — only allow actions the current stage permits.
        allowed = _STAGE_ALLOWED[self._policy.rollout_stage]
        if normalised_action not in allowed:
            return SafetyDecision(
                status="reject",
                reason=(
                    f"action {normalised_action!r} not permitted in rollout "
                    f"stage {self._policy.rollout_stage!r}"
                ),
            )

        # 2. Read-only actions: nothing else to check; allow immediately.
        if not _is_mutating_action(normalised_action):
            return SafetyDecision(status="allow", reason="read-only")

        # 2a. Mutating action requires the appropriate snapshot present.
        #     Without this guard an empty ReviewContext would route every
        #     per-op check to "skipped" and produce an auto-allow. Auditor
        #     CRITICAL finding on this PR.
        needs_budget, needs_bid = _required_snapshots(normalised_action)
        missing: list[str] = []
        if needs_budget and context.budget_snapshot is None:
            missing.append("budget_snapshot")
        if needs_bid and context.bid_snapshot is None:
            missing.append("bid_snapshot")
        if missing:
            return SafetyDecision(
                status="reject",
                reason=(
                    f"mutating action {normalised_action!r} requires snapshot "
                    f"data that was not supplied: {missing}"
                ),
                skipped_checks=[],
            )

        # 3. System-level gatekeepers (KS#6 + KS#7). If either blocks,
        #    the entire plan is rejected regardless of per-op content.
        blocking: list[CheckResult] = []
        warnings: list[CheckResult] = []
        skipped: list[str] = []

        self._run_check(
            "conversion_integrity",
            lambda: (
                self._conversion_integrity.check(
                    context.conversions_baseline,
                    context.conversions_current,
                )
                if context.conversions_baseline is not None
                and context.conversions_current is not None
                else None
            ),
            blocking,
            warnings,
            skipped,
        )
        self._run_check(
            "query_drift",
            lambda: (
                self._query_drift.check(
                    context.queries_baseline,
                    context.queries_current,
                )
                if context.queries_baseline is not None and context.queries_current is not None
                else None
            ),
            blocking,
            warnings,
            skipped,
        )

        if blocking:
            return SafetyDecision(
                status="reject",
                reason="system-level gatekeeper blocked the plan",
                blocking_checks=blocking,
                warnings=warnings,
                skipped_checks=skipped,
            )

        # 4. Per-operation checks. Only run the ones whose data is
        #    present in the context.
        self._run_check(
            "budget_cap",
            lambda: (
                self._budget_cap.check(context.budget_snapshot, context.budget_changes)
                if context.budget_snapshot is not None
                else None
            ),
            blocking,
            warnings,
            skipped,
        )
        self._run_check(
            "max_cpc",
            lambda: (
                self._max_cpc.check(context.bid_snapshot, context.bid_changes)
                if context.bid_snapshot is not None
                else None
            ),
            blocking,
            warnings,
            skipped,
        )
        self._run_check(
            "negative_keyword_floor",
            lambda: (
                self._negative_floor.check(context.budget_snapshot, context.budget_changes)
                if context.budget_snapshot is not None and context.budget_changes
                else None
            ),
            blocking,
            warnings,
            skipped,
        )
        self._run_check(
            "quality_score_guard",
            lambda: (
                self._qs_guard.check(context.bid_snapshot, context.bid_changes)
                if context.bid_snapshot is not None and context.bid_changes
                else None
            ),
            blocking,
            warnings,
            skipped,
        )
        self._run_check(
            "budget_balance_drift",
            lambda: (
                self._balance_drift.check(
                    context.budget_baseline,
                    context.budget_snapshot,
                    context.budget_changes,
                )
                if context.budget_baseline is not None and context.budget_snapshot is not None
                else None
            ),
            blocking,
            warnings,
            skipped,
        )

        # 5. Cross-call TOCTOU guard (KS#4 follow-up). A bid increase
        #    that exceeds the session's prior approved ceiling is
        #    blocked even if the per-call snapshot check passed —
        #    otherwise an agent can walk a bid up one kopek at a time
        #    across many calls.
        toctou = self._check_session_bid_ceiling(context)
        if toctou is not None:
            blocking.append(toctou)

        if blocking:
            return SafetyDecision(
                status="reject",
                reason="one or more safety checks blocked the plan",
                blocking_checks=blocking,
                warnings=warnings,
                skipped_checks=skipped,
            )

        # 6. Approval tier — does the action need human confirmation?
        if self._requires_confirmation(normalised_action):
            return SafetyDecision(
                status="confirm",
                reason=(
                    f"action {normalised_action!r} requires explicit "
                    f"human approval under current policy"
                ),
                warnings=warnings,
                skipped_checks=skipped,
            )

        # 7. (Session-bid register is updated by the executor via
        #    ``on_applied`` *after* the API call succeeds, not here.
        #    Recording at review time would poison the TOCTOU guard on
        #    executor failure — auditor HIGH on this PR.)

        return SafetyDecision(
            status="allow",
            reason="all checks passed",
            warnings=warnings,
            skipped_checks=skipped,
        )

    def on_applied(self, context: ReviewContext) -> None:
        """Callback for the executor to invoke *after* the plan's write
        succeeded against the live API.

        Updates the session TOCTOU register so the next review sees the
        actual new baseline. Called with the same ``context`` that
        ``review`` saw — in particular, the same ``bid_changes`` list.

        Executor must NOT call this on failed writes. Doing so poisons
        the TOCTOU register with a ceiling that isn't reflected in the
        actual account state, causing legitimate retries to look like
        fresh increases.
        """
        for bid in context.bid_changes:
            ceiling = max(
                bid.new_search_bid_rub or 0.0,
                bid.new_network_bid_rub or 0.0,
            )
            if ceiling > 0:
                self._session.record_approved_bid(bid.keyword_id, ceiling)

    # ------------------------------------------------------------------
    # Internals.
    # ------------------------------------------------------------------

    @staticmethod
    def _run_check(
        name: str,
        runner: Callable[[], CheckResult | None],
        blocking: list[CheckResult],
        warnings: list[CheckResult],
        skipped: list[str],
    ) -> None:
        """Dispatch to a single check; route result into the right bucket.

        A ``None`` return means "data missing — skip" and the check name is
        added to ``skipped``. Otherwise the ``CheckResult`` is routed by
        status: ``blocked`` → blocking list, ``warn`` → warnings list,
        ``ok`` → dropped (only non-ok results surface to callers).
        """
        result = runner()
        if result is None:
            skipped.append(name)
            return
        if result.status == "blocked":
            blocking.append(result)
        elif result.status == "warn":
            warnings.append(result)

    def _check_session_bid_ceiling(self, context: ReviewContext) -> CheckResult | None:
        """Raise a block if any bid_change exceeds the session's prior approval."""
        for bid in context.bid_changes:
            ceiling = max(
                bid.new_search_bid_rub or 0.0,
                bid.new_network_bid_rub or 0.0,
            )
            prior = self._session.approved_bid_ceiling(bid.keyword_id)
            if prior is not None and ceiling > prior:
                return CheckResult.blocked_result(
                    (
                        f"keyword {bid.keyword_id}: proposed bid "
                        f"{ceiling} exceeds session-approved ceiling "
                        f"{prior} (cross-call TOCTOU guard)"
                    ),
                    keyword_id=bid.keyword_id,
                    proposed_rub=ceiling,
                    session_ceiling_rub=prior,
                )
        return None

    def _requires_confirmation(self, action: str) -> bool:
        """Return True when the action needs explicit human approval.

        Policy exposes three auto-approve knobs today (pause, resume,
        negatives). The pipeline consults those for the matching action
        class.

        **Default posture for every other mutating action is confirm.**
        Kill-switches catching the dangerous cases is necessary but not
        sufficient — budget edits, bid edits, keyword creation, and
        campaign/ad-group/ad structure changes all produce durable
        state on a real money-spending account and should route past
        an operator by default. Operators who want to auto-approve a
        class (e.g. nightly-reportable budget edits within policy
        thresholds) can opt in when the next policy knob lands.

        This closes auditor HIGH on this PR.
        """
        # Whitelist paths: the three § M2.1 tiers that can auto-approve.
        if action == "pause_campaigns":
            return not self._policy.auto_approve_pause
        if action == "resume_campaigns":
            return not self._policy.auto_approve_resume
        if action == "add_negative_keywords":
            return not self._policy.auto_approve_negative_keywords

        # Whitelisted auto-approvable actions handled above. Every
        # other mutating action requires confirm until a dedicated
        # policy knob is added.
        return action not in _AUTO_APPROVABLE_ACTIONS
