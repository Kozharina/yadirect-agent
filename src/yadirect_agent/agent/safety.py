"""Safety layer — kill-switches, policy, plan→confirm→execute.

This PR (M2 kill-switch #1) implements only the budget-cap slice:

- ``BudgetCapPolicy`` — the subset of the full policy schema relevant to
  kill-switch #1 (see ``docs/TECHNICAL_SPEC.md`` §M2.1 for the full schema
  that lands in M2.1).
- ``AccountBudgetSnapshot`` and ``BudgetChange`` — the data shapes the
  check operates on. Snapshot = what Direct says about the account right
  now; changes = what the agent wants to do.
- ``BudgetCapCheck`` — projects the changes onto the snapshot and blocks
  if any cap is breached.
- ``CheckResult`` — the canonical result shape that every future
  kill-switch will return.

Later milestones extend this file:
- M2.0 #2-#7: six more ``*Check`` classes alongside ``BudgetCapCheck``.
- M2.1: full ``Policy`` model (currently a narrow slice).
- M2.2: ``OperationPlan`` + ``@requires_plan`` decorator + pipeline that
  runs every check in sequence and blocks on the first failure.
- M2.3: audit sink wired into each check's invocation.

Skeleton commit — ``BudgetCapCheck.check`` is a stub that always returns
``blocked`` so tests fail on the specific wrong answer (right reason)
rather than ``ImportError`` (wrong reason).
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

CheckStatus = Literal["ok", "blocked", "warn"]


# --------------------------------------------------------------------------
# CheckResult — shared by every kill-switch.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CheckResult:
    """Outcome of a single safety check.

    - ``ok``: operation may proceed.
    - ``blocked``: operation must not proceed. ``reason`` is surfaced to
      the human / agent as a user-visible message.
    - ``warn``: operation may proceed but something looks odd (e.g.
      approaching a cap); logged but does not stop the pipeline.
    """

    status: CheckStatus
    reason: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def ok_result(cls, **details: Any) -> CheckResult:
        return cls(status="ok", reason=None, details=details)

    @classmethod
    def blocked_result(cls, reason: str, **details: Any) -> CheckResult:
        return cls(status="blocked", reason=reason, details=details)

    @classmethod
    def warn_result(cls, reason: str, **details: Any) -> CheckResult:
        return cls(status="warn", reason=reason, details=details)


# --------------------------------------------------------------------------
# Account-level data shapes.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CampaignBudget:
    """Snapshot of one campaign's safety-relevant state.

    Used by every kill-switch that needs per-campaign context:
    - KS#1 (budget caps) reads ``daily_budget_rub`` + ``state`` + ``group``.
    - KS#3 (negative-keyword floor) reads ``negative_keywords``.

    Future kill-switches can add fields with defaults; existing tests
    stay green because nothing constructs this shape positionally.
    """

    id: int
    name: str
    daily_budget_rub: float
    state: str  # "ON" | "SUSPENDED" | "OFF" | "ENDED" | ...
    group: str | None = None  # None = no group label; unscoped by group caps
    negative_keywords: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class AccountBudgetSnapshot:
    """Current state of every campaign whose budget we care about."""

    campaigns: list[CampaignBudget] = field(default_factory=list)

    def total_active_budget_rub(self) -> float:
        """Sum of daily budgets of campaigns that are actually spending (state=ON)."""
        return sum(c.daily_budget_rub for c in self.campaigns if c.state == "ON")

    def group_active_budget_rub(self, group: str) -> float:
        """Sum of daily budgets of ON campaigns assigned to ``group``."""
        return sum(
            c.daily_budget_rub for c in self.campaigns if c.state == "ON" and c.group == group
        )


# Direct states we recognise. Kept as a Literal (not a StrEnum) so
# pydantic produces a tight schema error on typos like "on" / "enabled".
BudgetChangeState = Literal["ON", "OFF", "SUSPENDED", "ENDED", "CONVERTED", "ARCHIVED"]


class BudgetChange(BaseModel):
    """A proposed change to a single campaign's budget-relevant state.

    A field set to ``None`` means "leave that property as-is". This lets
    a single object describe a budget change, a resume/pause, or both.

    Validated at construction (security-auditor review, HIGH findings):
    - ``new_daily_budget_rub`` must be ``>= 0``; negatives would shrink
      the projected total and bypass the cap.
    - ``new_state`` must match Direct's actual enum; free strings like
      ``"on"`` would bypass the ``state == "ON"`` filter in totals.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    campaign_id: int
    new_daily_budget_rub: float | None = Field(default=None, ge=0)
    new_state: BudgetChangeState | None = None


# --------------------------------------------------------------------------
# Policy schema (budget-cap slice only — full Policy in M2.1).
# --------------------------------------------------------------------------


class BudgetCapPolicy(BaseModel):
    """Kill-switch #1 policy slice.

    ``account_daily_budget_cap_rub`` is mandatory — the agent refuses to
    run without an explicit account ceiling. ``campaign_group_caps_rub``
    is optional; missing keys mean a group is unconstrained (bounded
    only by the account cap).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    account_daily_budget_cap_rub: int = Field(
        ...,
        ge=0,
        description="Hard ceiling on the sum of active campaign daily budgets.",
    )
    campaign_group_caps_rub: dict[str, int] = Field(
        default_factory=dict,
        description="Optional per-group ceilings (group name → RUB).",
    )


def _find_duplicate_ids(changes: list[BudgetChange]) -> list[int]:
    """Return campaign_ids that appear more than once in ``changes``, in
    the order of their first duplicate occurrence. Empty list means
    every id is unique."""
    seen: set[int] = set()
    dupes: list[int] = []
    for c in changes:
        if c.campaign_id in seen and c.campaign_id not in dupes:
            dupes.append(c.campaign_id)
        seen.add(c.campaign_id)
    return dupes


def load_budget_cap_policy(path: Path) -> BudgetCapPolicy:
    """Read ``agent_policy.yml`` and extract the budget-cap slice.

    Parses the full YAML but only validates the fields this PR cares
    about. Unknown top-level keys are tolerated — M2.1 will land the
    remaining fields without breaking existing files.
    """
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    budget_fields = {
        k: raw[k] for k in ("account_daily_budget_cap_rub", "campaign_group_caps_rub") if k in raw
    }
    return BudgetCapPolicy.model_validate(budget_fields)


# --------------------------------------------------------------------------
# BudgetCapCheck — kill-switch #1.
# --------------------------------------------------------------------------


class BudgetCapCheck:
    """Block plans that would push daily spend over a configured cap.

    Pipeline:
    1. Apply ``changes`` to a copy of ``snapshot`` (state + budget fields).
    2. Compute the projected total active spend. Block if > account cap.
    3. For every group that has a cap configured, compute its projected
       total active spend. Block on the first violation.
    4. Otherwise ok.

    Suspended / OFF campaigns are *excluded* from the totals even if
    their budget changes — the concern is today's spend, not potential
    future spend. Flipping state (e.g. SUSPENDED → ON via
    ``BudgetChange.new_state``) is first-class: it moves a campaign
    into or out of the total.
    """

    def __init__(self, policy: BudgetCapPolicy) -> None:
        self._policy = policy

    def check(
        self,
        snapshot: AccountBudgetSnapshot,
        changes: list[BudgetChange],
    ) -> CheckResult:
        duplicates = _find_duplicate_ids(changes)
        if duplicates:
            # security-auditor HIGH finding: `_project` would silently
            # keep only the last BudgetChange for a given id, letting an
            # adversarial caller hide a budget spike behind a later
            # state flip. Refuse the whole batch instead.
            first = duplicates[0]
            return CheckResult.blocked_result(
                f"duplicate campaign_id in changes: {first}",
                campaign_id=first,
                duplicates=duplicates,
            )

        projected = self._project(snapshot, changes)

        account_total = projected.total_active_budget_rub()
        account_cap = self._policy.account_daily_budget_cap_rub
        if account_total > account_cap:
            return CheckResult.blocked_result(
                "account daily budget cap would be exceeded",
                projected_rub=account_total,
                cap_rub=account_cap,
            )

        for group, group_cap in self._policy.campaign_group_caps_rub.items():
            group_total = projected.group_active_budget_rub(group)
            if group_total > group_cap:
                return CheckResult.blocked_result(
                    f"campaign-group daily budget cap would be exceeded: {group!r}",
                    group=group,
                    projected_rub=group_total,
                    cap_rub=group_cap,
                )

        return CheckResult.ok_result(
            projected_total_rub=account_total,
            account_cap_rub=account_cap,
        )

    @staticmethod
    def _project(
        snapshot: AccountBudgetSnapshot,
        changes: list[BudgetChange],
    ) -> AccountBudgetSnapshot:
        # Duplicate-id rejection happens in `check()` before this point,
        # so building a dict here is safe.
        """Return a new snapshot with every change applied.

        Changes not matching any existing campaign are silently ignored
        — the agent sometimes proposes an id that got archived between
        the snapshot read and the policy check. We don't synthesise
        phantom campaigns; the calling layer can re-read and re-plan.
        """
        by_id: dict[int, BudgetChange] = {c.campaign_id: c for c in changes}
        next_campaigns: list[CampaignBudget] = []
        for c in snapshot.campaigns:
            change = by_id.get(c.id)
            if change is None:
                next_campaigns.append(c)
                continue
            new_budget = (
                change.new_daily_budget_rub
                if change.new_daily_budget_rub is not None
                else c.daily_budget_rub
            )
            new_state = change.new_state if change.new_state is not None else c.state
            next_campaigns.append(
                CampaignBudget(
                    id=c.id,
                    name=c.name,
                    daily_budget_rub=new_budget,
                    state=new_state,
                    group=c.group,
                )
            )
        return AccountBudgetSnapshot(campaigns=next_campaigns)


# --------------------------------------------------------------------------
# Kill-switch #2 — Max CPC per campaign.
#
# Blocks bid updates that would push a keyword's CPC above the cap
# configured for its owning campaign. Independent of BudgetCapCheck:
# shares only `CheckResult` and the auditor-driven validation style
# (no negative bids, no duplicate ids, no unknown fields).
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class KeywordSnapshot:
    """Snapshot of one keyword's safety-relevant state.

    - KS#2 (max CPC) reads ``campaign_id`` + current bids.
    - KS#4 (QS guardrail) reads ``quality_score`` + current bids (to
      detect increases).

    ``quality_score`` is a Direct 0-10 score; ``None`` means unknown
    (new keyword, Direct hasn't scored it yet). KS#4 treats unknown
    QS as "skip, don't block" — a missing signal is not evidence of
    a low QS.

    Bid fields ``current_search_bid_rub`` / ``current_network_bid_rub``:
    ``None`` denotes "unknown at snapshot time" rather than "not
    applicable". KS#2 and KS#4 both defer (skip the check) when the
    current value is None — they cannot prove an increase or a cap
    violation without a base value. An agent presenting a partial
    snapshot therefore routes around the guard; mitigations are
    tracked in BACKLOG (M2.3 audit surfaces every deferred-None case
    as a warn; the snapshot builder must read bids eagerly).

    ``__post_init__`` enforces the QS type contract at construction —
    frozen dataclass offers no runtime validation on its own, so a
    caller could otherwise slip ``quality_score=4.5`` past us and
    undermine the ``>= threshold`` comparison. See
    tests/unit/agent/test_safety.py for the pinned edge cases.
    """

    keyword_id: int
    campaign_id: int
    current_search_bid_rub: float | None = None
    current_network_bid_rub: float | None = None
    quality_score: int | None = None

    def __post_init__(self) -> None:
        if self.quality_score is None:
            return
        # `bool` is a subclass of `int` in Python; reject it explicitly
        # so a stray `True`/`False` from a JSON mapper doesn't become
        # a `1` or `0` QS value that passes every threshold trivially.
        if isinstance(self.quality_score, bool) or not isinstance(self.quality_score, int):
            msg = (
                "KeywordSnapshot.quality_score must be int or None, "
                f"got {type(self.quality_score).__name__}"
            )
            raise TypeError(msg)
        if not 0 <= self.quality_score <= 10:
            msg = f"KeywordSnapshot.quality_score must be in range 0..10, got {self.quality_score}"
            raise ValueError(msg)


@dataclass(frozen=True)
class AccountBidSnapshot:
    """Every keyword we care about, with its owning campaign and bids."""

    keywords: list[KeywordSnapshot] = field(default_factory=list)

    def find(self, keyword_id: int) -> KeywordSnapshot | None:
        """Return the snapshot entry for ``keyword_id`` or None."""
        for k in self.keywords:
            if k.keyword_id == keyword_id:
                return k
        return None


class ProposedBidChange(BaseModel):
    """Safety-layer bid-change proposal.

    Deliberately *not* ``services.bidding.BidUpdate`` — safety is a
    lower layer than services, so the caller maps its own DTO onto
    this one before running the check. Validation constraints come
    from the HIGH findings security-auditor raised on KS#1:
    - negative bids would shift the comparison and bypass the cap
    - extra="forbid" so future fields need explicit support
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    keyword_id: int
    new_search_bid_rub: float | None = Field(default=None, ge=0)
    new_network_bid_rub: float | None = Field(default=None, ge=0)


class MaxCpcPolicy(BaseModel):
    """Kill-switch #2 policy slice.

    ``campaign_max_cpc_rub`` maps campaign_id → hard cap on any single
    bid (search or network) within that campaign. A campaign without
    an entry is unconstrained *by this check* (the account-level
    BudgetCapCheck still applies). There is no global default cap —
    if a cap matters, it must be set explicitly.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    campaign_max_cpc_rub: dict[int, float] = Field(
        default_factory=dict,
        description="Max allowed single-bid value per campaign_id, in RUB.",
    )


def load_max_cpc_policy(path: Path) -> MaxCpcPolicy:
    """Read ``agent_policy.yml`` and extract the max-CPC slice.

    Unknown top-level keys are tolerated so the same YAML file can
    carry fields for every kill-switch without needing a matching
    loader per slice.
    """
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    fields = {k: raw[k] for k in ("campaign_max_cpc_rub",) if k in raw}
    return MaxCpcPolicy.model_validate(fields)


def _find_duplicate_keyword_ids(updates: list[ProposedBidChange]) -> list[int]:
    """Return keyword_ids that appear more than once in ``updates``,
    in order of first duplicate occurrence. Empty list means unique."""
    seen: set[int] = set()
    dupes: list[int] = []
    for u in updates:
        if u.keyword_id in seen and u.keyword_id not in dupes:
            dupes.append(u.keyword_id)
        seen.add(u.keyword_id)
    return dupes


class MaxCpcCheck:
    """Block bid updates that would put CPC above the per-campaign cap.

    Pipeline:
    1. Reject the whole batch if any keyword_id appears twice in
       ``updates`` — matches BudgetCapCheck's same-style refusal.
    2. For each update, look up the keyword's campaign in the snapshot.
       Unknown keyword_ids are silently skipped (tech-debt: surface as
       a warn detail when M2.3 audit lands).
    3. Pull the campaign's cap from policy. A campaign with no entry is
       unconstrained by this check — the account-level BudgetCapCheck
       still applies.
    4. Compare proposed search and network bids against the cap. Block
       on the first strict ``> cap`` violation; equality is ok.

    Bid validation (``>= 0``, unknown fields, frozen instances) already
    happened at ``ProposedBidChange`` construction.
    """

    def __init__(self, policy: MaxCpcPolicy) -> None:
        self._policy = policy

    def check(
        self,
        snapshot: AccountBidSnapshot,
        updates: list[ProposedBidChange],
    ) -> CheckResult:
        duplicates = _find_duplicate_keyword_ids(updates)
        if duplicates:
            first = duplicates[0]
            return CheckResult.blocked_result(
                f"duplicate keyword_id in updates: {first}",
                keyword_id=first,
                duplicates=duplicates,
            )

        for u in updates:
            kw = snapshot.find(u.keyword_id)
            if kw is None:
                # Keyword disappeared between snapshot read and check —
                # pass for now; TODO (BACKLOG): warn-level detail via
                # the audit sink once M2.3 exists.
                continue
            cap = self._policy.campaign_max_cpc_rub.get(kw.campaign_id)
            if cap is None:
                # Campaign has no configured cap — unconstrained by this
                # check. BudgetCapCheck still enforces account-level.
                continue

            if u.new_search_bid_rub is not None and u.new_search_bid_rub > cap:
                return CheckResult.blocked_result(
                    f"search bid exceeds max CPC cap for campaign {kw.campaign_id}",
                    keyword_id=u.keyword_id,
                    campaign_id=kw.campaign_id,
                    bid_type="search",
                    proposed_rub=u.new_search_bid_rub,
                    cap_rub=cap,
                )
            if u.new_network_bid_rub is not None and u.new_network_bid_rub > cap:
                return CheckResult.blocked_result(
                    f"network bid exceeds max CPC cap for campaign {kw.campaign_id}",
                    keyword_id=u.keyword_id,
                    campaign_id=kw.campaign_id,
                    bid_type="network",
                    proposed_rub=u.new_network_bid_rub,
                    cap_rub=cap,
                )

        return CheckResult.ok_result()


# --------------------------------------------------------------------------
# Kill-switch #3 — Negative-keyword floor.
#
# Refuses to resume a campaign that does not carry every phrase in the
# required-negatives list. Source: docs/TECHNICAL_SPEC.md §M2.0 rule 3
# and docs/PRIOR_ART.md "Agentic PPC Campaign Management". Reuses the
# KS#1 data shapes (CampaignBudget, AccountBudgetSnapshot, BudgetChange)
# because every resume is already represented there as
# BudgetChange(new_state="ON").
# --------------------------------------------------------------------------


class NegativeKeywordFloorPolicy(BaseModel):
    """Kill-switch #3 policy slice.

    Matching is case-insensitive, whitespace-trimmed, and
    Unicode-normalised (NFC) — an operator writing ``"Бесплатно"`` in
    YAML must match a campaign's existing ``"бесплатно "`` without
    manual curation, and NFC/NFD encoding differences from the API
    must not create false mismatches.

    Empty/whitespace-only entries are rejected at load time: a ``""``
    would collapse to an empty required-set element, blocking every
    resume and creating a denial-of-service on the safety gate
    (security-auditor MEDIUM finding on KS#3).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    required_negative_keywords: list[str] = Field(
        default_factory=list,
        description="Phrases every campaign must carry before being resumed.",
    )

    @field_validator("required_negative_keywords")
    @classmethod
    def _reject_blank_entries(cls, values: list[str]) -> list[str]:
        for v in values:
            if not v or not v.strip():
                msg = "required_negative_keywords must not contain empty or whitespace-only entries"
                raise ValueError(msg)
        return values


def load_negative_keyword_floor_policy(path: Path) -> NegativeKeywordFloorPolicy:
    """Read ``agent_policy.yml`` and extract the negative-keyword floor slice."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    fields = {k: raw[k] for k in ("required_negative_keywords",) if k in raw}
    return NegativeKeywordFloorPolicy.model_validate(fields)


def _normalize_keyword(phrase: str) -> str:
    """Fold a phrase for case-insensitive, whitespace-forgiving,
    Unicode-canonicalised comparison.

    Normalisation order: NFC first (so NFD-decomposed input from the
    API folds to the canonical composed form), then strip whitespace,
    then lowercase. Keeps safety-layer independent of
    services.semantics.

    Without the NFC step, a campaign keyword returned by the Direct
    API in NFD form (where each combining letter like U+0439 CYRILLIC
    SMALL LETTER SHORT I decomposes into U+0438 + U+0306) would not
    match a policy written in the composed NFC form. That's a silent
    false-positive block in one direction and a potential false
    negative in the other. Auditor HIGH finding on KS#3.
    """
    return unicodedata.normalize("NFC", phrase).strip().lower()


class NegativeKeywordFloorCheck:
    """Block resume operations on campaigns lacking the required negatives.

    Pipeline:
    1. Reject the whole batch if any campaign_id appears twice in
       ``changes`` (shared guard with KS#1/#2).
    2. If the policy's required list is empty, every resume is fine.
    3. For each change whose ``new_state == "ON"`` (a resume), look
       the campaign up in the snapshot. Unknown ids skip silently
       (BACKLOG item covers the warn-level surfacing).
    4. Normalise required and existing negatives (strip + lower), then
       verify that the campaign's set is a superset of the required.
       Block on the first campaign missing anything.

    Non-resume changes (pause, budget-only) are not this kill-switch's
    concern and pass through.
    """

    def __init__(self, policy: NegativeKeywordFloorPolicy) -> None:
        self._policy = policy

    def check(
        self,
        snapshot: AccountBudgetSnapshot,
        changes: list[BudgetChange],
    ) -> CheckResult:
        duplicates = _find_duplicate_ids(changes)
        if duplicates:
            first = duplicates[0]
            return CheckResult.blocked_result(
                f"duplicate campaign_id in changes: {first}",
                campaign_id=first,
                duplicates=duplicates,
            )

        required = {_normalize_keyword(p) for p in self._policy.required_negative_keywords}
        if not required:
            return CheckResult.ok_result()

        by_id = {c.id: c for c in snapshot.campaigns}
        for change in changes:
            if change.new_state != "ON":
                continue
            campaign = by_id.get(change.campaign_id)
            if campaign is None:
                # tech-debt: surface as warn in M2.3 audit.
                continue
            existing = {_normalize_keyword(kw) for kw in campaign.negative_keywords}
            missing = sorted(required - existing)
            if missing:
                return CheckResult.blocked_result(
                    (
                        f"campaign {campaign.id} is missing required negative "
                        f"keywords: {', '.join(missing)}"
                    ),
                    campaign_id=campaign.id,
                    missing=missing,
                )

        return CheckResult.ok_result()


# --------------------------------------------------------------------------
# Kill-switch #4 — Quality Score guardrail.
#
# QS is a *constraint*, not an objective. Refuse to raise a bid on a
# keyword whose current QS is below policy, because raising CPC on a
# low-QS keyword both wastes money (low QS → high actual CPC per click)
# and risks further QS degradation at serving time. Source:
# docs/TECHNICAL_SPEC.md §M2.0 rule 4 and §M2.6.
#
# Explicitly *not* in this PR (see BACKLOG):
# - §M2.6 campaign-median QS tracking over 7 days.
# - Alert emission when medial QS drops >1 point.
# Those need historical snapshots; KS#4 here is the single-point
# gate that fires at plan-check time.
# --------------------------------------------------------------------------


class QualityScoreGuardPolicy(BaseModel):
    """Kill-switch #4 policy slice.

    ``min_quality_score_for_bid_increase`` is the Direct QS floor below
    which an explicit bid increase is refused. Direct QS is a 0-10
    integer; policy defaults to 5 per §M2.1.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    min_quality_score_for_bid_increase: int = Field(
        default=5,
        ge=0,
        le=10,
        description="Keywords with QS below this value may not be bid up.",
    )


def load_quality_score_guard_policy(path: Path) -> QualityScoreGuardPolicy:
    """Read ``agent_policy.yml`` and extract the QS-guardrail slice."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    fields = {k: raw[k] for k in ("min_quality_score_for_bid_increase",) if k in raw}
    return QualityScoreGuardPolicy.model_validate(fields)


class QualityScoreGuardCheck:
    """Block bid *increases* on keywords whose QS is below policy.

    Pipeline:
    1. Duplicate keyword_id in ``updates`` → reject the whole batch
       (shared guard with KS#1/#2/#3).
    2. For each update, look up the keyword. Unknown ids skip
       (consistent BACKLOG follow-up for M2.3 audit).
    3. If QS is unknown (None) — defer. Missing signal ≠ bad signal;
       operator should backfill QS before retrying.
    4. If QS ≥ threshold — nothing to say.
    5. QS < threshold: check each bid field against its current value
       in the snapshot. A strict increase is blocked; equality and
       decrease pass. If the current value is unknown, we can't tell
       if it's an increase, so we defer.
    6. Search field is checked before network — the order is part of
       the documented contract (pinned in tests).

    What never blocks:
    - Lowering or holding a bid (the operator is doing the right
      thing on a low-QS keyword).
    - Bid on a keyword the snapshot doesn't include (silent skip).
    - Bid when either current or new value is None on that field.
    """

    def __init__(self, policy: QualityScoreGuardPolicy) -> None:
        self._policy = policy

    def check(
        self,
        snapshot: AccountBidSnapshot,
        updates: list[ProposedBidChange],
    ) -> CheckResult:
        duplicates = _find_duplicate_keyword_ids(updates)
        if duplicates:
            first = duplicates[0]
            return CheckResult.blocked_result(
                f"duplicate keyword_id in updates: {first}",
                keyword_id=first,
                duplicates=duplicates,
            )

        threshold = self._policy.min_quality_score_for_bid_increase

        for u in updates:
            kw = snapshot.find(u.keyword_id)
            if kw is None:
                continue
            if kw.quality_score is None:
                continue
            if kw.quality_score >= threshold:
                continue

            # QS strictly below threshold — inspect each bid field for
            # an increase.
            if self._is_increase(u.new_search_bid_rub, kw.current_search_bid_rub):
                return CheckResult.blocked_result(
                    (
                        f"search bid increase on keyword {u.keyword_id} refused: "
                        f"QS {kw.quality_score} is below threshold {threshold}"
                    ),
                    keyword_id=u.keyword_id,
                    campaign_id=kw.campaign_id,
                    quality_score=kw.quality_score,
                    threshold=threshold,
                    bid_type="search",
                    current_rub=kw.current_search_bid_rub,
                    proposed_rub=u.new_search_bid_rub,
                )
            if self._is_increase(u.new_network_bid_rub, kw.current_network_bid_rub):
                return CheckResult.blocked_result(
                    (
                        f"network bid increase on keyword {u.keyword_id} refused: "
                        f"QS {kw.quality_score} is below threshold {threshold}"
                    ),
                    keyword_id=u.keyword_id,
                    campaign_id=kw.campaign_id,
                    quality_score=kw.quality_score,
                    threshold=threshold,
                    bid_type="network",
                    current_rub=kw.current_network_bid_rub,
                    proposed_rub=u.new_network_bid_rub,
                )

        return CheckResult.ok_result()

    @staticmethod
    def _is_increase(new: float | None, current: float | None) -> bool:
        """Return True if ``new`` strictly exceeds ``current``.

        If either side is None the answer is False — a missing value
        means we cannot prove an increase, so we defer rather than
        block.
        """
        if new is None or current is None:
            return False
        return new > current
