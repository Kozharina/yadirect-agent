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

import math
import re
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

    Matching is case-insensitive, whitespace-tolerant on *all*
    whitespace positions (leading, trailing, and internal runs
    between words), and Unicode-normalised (NFC) — an operator
    writing ``"Бесплатно скачать"`` in YAML must match a campaign's
    existing ``"бесплатно  скачать "`` without manual curation, and
    NFC/NFD encoding differences from the API must not create false
    mismatches.

    Internal-whitespace folding is shared with KS#7 via
    ``_normalize_keyword``. Multi-word phrases with stray double
    spaces fold to single spaces before comparison — see
    ``test_multi_word_negative_keyword_internal_whitespace_collapses``
    in the test suite for the pinned behaviour.

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


_INTERNAL_WS_RE = re.compile(r"\s+")


def _normalize_keyword(phrase: str) -> str:
    """Fold a phrase for case-insensitive, whitespace-forgiving,
    Unicode-canonicalised comparison.

    Normalisation order: NFC first (so NFD-decomposed input from the
    API folds to the canonical composed form), then collapse internal
    whitespace runs to a single space (multi-word search queries like
    "купить  обувь" must match "купить обувь"), then strip, then
    lowercase. Keeps safety-layer independent of services.semantics.

    Without the NFC step, a campaign keyword returned by the Direct
    API in NFD form (where each combining letter like U+0439 CYRILLIC
    SMALL LETTER SHORT I decomposes into U+0438 + U+0306) would not
    match a policy written in the composed NFC form. That's a silent
    false-positive block in one direction and a potential false
    negative in the other. Auditor HIGH finding on KS#3.

    Internal-whitespace collapse added for KS#7: search queries are
    multi-word and users / reports sometimes include stray extra
    spaces. The KS#3 tests still pass because they only use trailing/
    leading whitespace and single-word phrases.
    """
    return _INTERNAL_WS_RE.sub(" ", unicodedata.normalize("NFC", phrase)).strip().lower()


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
                # Privacy: ``reason`` is a free-form string that flows
                # into AuditEvent.result and AgentLoop tool responses;
                # the per-key blocklist in audit.py cannot redact it.
                # Operator-supplied negative keyword phrases may carry
                # brand / competitor / sensitive terms — surface only
                # the count here. ``details["missing"]`` still carries
                # the full list for in-process inspection but the audit
                # sink strips it via ``_PRIVATE_KEYS``. M2.3a auditor
                # M-2 follow-up.
                return CheckResult.blocked_result(
                    (
                        f"campaign {campaign.id} is missing "
                        f"{len(missing)} required negative keyword(s)"
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


# --------------------------------------------------------------------------
# Kill-switch #5 — Budget-balance drift.
#
# Refuses plans that would shift a single campaign's share of the daily
# account budget by more than a configured number of percentage points
# vs. a baseline (yesterday's) distribution. Protects against the
# "agent poured everything into one campaign overnight" failure mode.
# Source: docs/TECHNICAL_SPEC.md §M2.0 rule 5.
#
# First kill-switch with a temporal dimension: the check takes two
# snapshots (baseline + current) and projects changes onto the current
# to produce a "projected" distribution, then compares per-campaign
# shares. Distances are in absolute percentage points.
#
# Out of scope for this PR (BACKLOG): historical snapshot store with
# rolling windows, cross-day baseline rotation, alerts on sustained
# drift. Here the check is a single-call function that treats the
# `baseline` argument as ground truth.
# --------------------------------------------------------------------------


class BudgetBalanceDriftPolicy(BaseModel):
    """Kill-switch #5 policy slice.

    ``max_shift_pct_per_day`` is the hard ceiling on how much any
    single campaign's share of the active daily budget may move in
    absolute percentage points between yesterday's baseline and the
    projected state after changes apply. A default of ``0.3`` matches
    §M2.1 (a 30-percentage-point shift is already enough to materially
    restructure an account's delivery profile).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_shift_pct_per_day: float = Field(
        default=0.3,
        gt=0,
        le=1,
        description=(
            "Max absolute change in a campaign's share of account-level "
            "active budget, expressed as a [0, 1] fraction of total."
        ),
    )


def load_budget_balance_drift_policy(path: Path) -> BudgetBalanceDriftPolicy:
    """Read ``agent_policy.yml`` and extract the balance-drift slice."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    fields = {k: raw[k] for k in ("max_shift_pct_per_day",) if k in raw}
    return BudgetBalanceDriftPolicy.model_validate(fields)


class BudgetBalanceDriftCheck:
    """Block plans that redistribute account budget too aggressively.

    Pipeline:
    1. Duplicate campaign_id in ``changes`` → reject whole batch.
    2. Project ``changes`` onto ``snapshot`` (reuse BudgetCapCheck's
       projection semantics: state flips first-class, unknown ids
       silently dropped).
    3. Compute baseline active total and projected active total.
       If either is zero, defer — no reference distribution, any
       allocation today is "new" and shouldn't be called drift.
    4. For every campaign that appears in baseline or projected,
       compute its share in each, and take the absolute difference.
       Campaigns missing from one side contribute share = 0 on that
       side.
    5. Strictly greater than ``max_shift_pct_per_day`` → block on
       that first campaign; equality is ok.

    Only ``state == "ON"`` campaigns contribute to the totals —
    suspended or ended campaigns don't spend, so their budget numbers
    are noise for drift. Pausing a previously-active campaign moves
    its share from its prior value to 0 (which IS a drift the operator
    may want to block on).
    """

    def __init__(self, policy: BudgetBalanceDriftPolicy) -> None:
        self._policy = policy

    def check(
        self,
        baseline: AccountBudgetSnapshot,
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

        projected = BudgetCapCheck._project(snapshot, changes)

        baseline_total = baseline.total_active_budget_rub()
        projected_total = projected.total_active_budget_rub()

        # Empty baseline is a real risk window — first autonomous run
        # or a baseline that wasn't backfilled would otherwise silently
        # pass every rebalance. We emit `warn` (not `ok`) so the
        # decision surfaces in the audit sink (M2.3) and the pipeline
        # layer (M2.2) can refuse autonomous operation until a real
        # baseline is present. See security-auditor LOW on KS#5.
        if baseline_total == 0:
            return CheckResult.warn_result(
                "baseline has no active budget; drift check cannot apply",
                baseline_total_rub=baseline_total,
                projected_total_rub=projected_total,
            )
        if projected_total == 0:
            # Everything paused — nothing is spending, drift is
            # mathematically undefined. That's safe, not risky.
            return CheckResult.ok_result(
                baseline_total_rub=baseline_total,
                projected_total_rub=projected_total,
            )

        threshold = self._policy.max_shift_pct_per_day

        baseline_active = {
            c.id: c.daily_budget_rub / baseline_total for c in baseline.campaigns if c.state == "ON"
        }
        projected_active = {
            c.id: c.daily_budget_rub / projected_total
            for c in projected.campaigns
            if c.state == "ON"
        }

        all_ids = sorted(baseline_active.keys() | projected_active.keys())
        for cid in all_ids:
            before = baseline_active.get(cid, 0.0)
            after = projected_active.get(cid, 0.0)
            shift = abs(after - before)
            # IEEE 754: values that should mathematically equal
            # threshold often come out slightly above due to
            # rounding (e.g. 0.8-0.5 ≈ 0.30000000000000004).
            # `math.isclose` gives us a tolerance so "exactly at
            # threshold" inputs stay ok instead of flickering blocked.
            # Tolerance tightened from abs_tol=1e-12 to 1e-14 after
            # security-auditor LOW finding: the wider window let
            # `threshold + 1e-11` slip past math.isclose. Legitimate
            # IEEE 754 rounding on real budget inputs (integer rubles
            # divided by integer totals) stays inside 1e-14.
            if shift > threshold and not math.isclose(
                shift, threshold, rel_tol=1e-9, abs_tol=1e-14
            ):
                return CheckResult.blocked_result(
                    (
                        f"campaign {cid} share would shift "
                        f"{shift * 100:.1f}pp (threshold "
                        f"{threshold * 100:.0f}pp)"
                    ),
                    campaign_id=cid,
                    shift_pct=shift,
                    threshold=threshold,
                    baseline_share=before,
                    projected_share=after,
                )

        return CheckResult.ok_result(
            baseline_total_rub=baseline_total,
            projected_total_rub=projected_total,
        )


# --------------------------------------------------------------------------
# Kill-switch #6 — Conversion integrity.
#
# Unlike KS#1-#5, this is not a per-operation guard — it's a
# system-level gatekeeper that runs once before any write plan and
# blocks *every* write when Metrika tracking looks broken. Three
# classes of failure:
#   1. Volume collapse — current conversions are far below baseline,
#      suggesting the tracking pixel died, a tag manager update wiped
#      it, or a privacy-setting change killed collection.
#   2. Absolute floor — current conversions below a minimum count per
#      window, catching "zero conversions for a whole day" cases.
#   3. Missing goals — a goal that existed in baseline is not in
#      the current snapshot, suggesting it was deleted or misconfigured.
#
# The check's signature is (baseline, current) — no `changes` list.
# The pipeline runner (M2.2) reads the result; a `blocked` result
# aborts the whole plan before any write executes. A `warn` result
# (e.g. baseline empty on first-ever run) is surfaced but does not
# itself block writes — M2.3 audit sink records it.
#
# Out of scope here (BACKLOG): real Metrika integration
# (`MetrikaService.get_report` in M6), rolling-window arithmetic,
# per-goal sensitivity tuning.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class GoalConversions:
    """Conversions for one Metrika goal over the snapshot window.

    ``__post_init__`` enforces the count contract at construction.
    Without it, a plain frozen dataclass would accept negative ints
    and booleans — a caller stitching a snapshot from a malicious
    or corrupt Metrika response could submit `conversions=-500`,
    inflating the ratio-vs-baseline metric in their favour and
    bypassing KS#6. Same pattern as KeywordSnapshot.__post_init__
    for quality_score (security-auditor LOW on KS#6).
    """

    goal_id: int
    goal_name: str
    conversions: int

    def __post_init__(self) -> None:
        # bool is a subclass of int — reject explicitly so True/False
        # don't slip in as 1/0.
        if isinstance(self.conversions, bool) or not isinstance(self.conversions, int):
            msg = f"GoalConversions.conversions must be int, got {type(self.conversions).__name__}"
            raise TypeError(msg)
        if self.conversions < 0:
            msg = f"GoalConversions.conversions must be non-negative, got {self.conversions}"
            raise ValueError(msg)


@dataclass(frozen=True)
class ConversionsSnapshot:
    """Metrika conversions observed over one reporting window.

    The window size and anchor are the caller's concern; this shape
    only carries the counts. The check compares two snapshots for
    the same counter_id — it does not attempt to reason about time.
    """

    counter_id: int
    goals: list[GoalConversions] = field(default_factory=list)

    def total_conversions(self) -> int:
        return sum(g.conversions for g in self.goals)

    def goal_ids(self) -> set[int]:
        return {g.goal_id for g in self.goals}

    def find(self, goal_id: int) -> GoalConversions | None:
        for g in self.goals:
            if g.goal_id == goal_id:
                return g
        return None


class ConversionIntegrityPolicy(BaseModel):
    """Kill-switch #6 policy slice.

    Three knobs, each can be disabled independently:

    - ``min_conversions_total`` — absolute floor on ``current``'s
      total count. Zero means the floor is disabled.
    - ``min_ratio_vs_baseline`` — ratio current/baseline must be ≥
      this. Zero disables the ratio check; 1.0 demands no drop at
      all (rarely useful).
    - ``require_all_baseline_goals_present`` — if True, every
      goal_id in baseline must exist in current (current may have
      new goals — additive changes are fine).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    min_conversions_total: int = Field(default=1, ge=0)
    min_ratio_vs_baseline: float = Field(default=0.5, ge=0.0, le=1.0)
    require_all_baseline_goals_present: bool = True


def load_conversion_integrity_policy(path: Path) -> ConversionIntegrityPolicy:
    """Read ``agent_policy.yml`` and extract the conversion-integrity slice."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    keys = (
        "min_conversions_total",
        "min_ratio_vs_baseline",
        "require_all_baseline_goals_present",
    )
    fields = {k: raw[k] for k in keys if k in raw}
    return ConversionIntegrityPolicy.model_validate(fields)


class ConversionIntegrityCheck:
    """Block all writes when Metrika tracking looks broken.

    Pipeline (fails fast on the first tripped rule):
    0. Empty baseline or baseline total == 0 → warn. No reference
       point for the ratio; don't silently pass.
    1. Absolute floor: current total < min_conversions_total →
       blocked. Detects "zero conversions for a window" outages.
    2. Ratio: current / baseline < min_ratio_vs_baseline → blocked.
       Detects collapses that the absolute floor alone misses.
    3. Missing goals: baseline goal_ids not ⊆ current goal_ids →
       blocked (unless require_all_baseline_goals_present is False).
       Detects deleted / misconfigured goals. Additive changes
       (new goals in current) are fine.
    """

    def __init__(self, policy: ConversionIntegrityPolicy) -> None:
        self._policy = policy

    def check(
        self,
        baseline: ConversionsSnapshot,
        current: ConversionsSnapshot,
    ) -> CheckResult:
        # Sanity gate: the two snapshots must describe the same
        # Metrika counter. Comparing tracking from counter A against
        # counter B is meaningless and a realistic pipeline-wiring
        # mistake in M2.2 (security-auditor LOW on KS#6).
        if baseline.counter_id != current.counter_id:
            return CheckResult.blocked_result(
                (
                    f"snapshot counter_id mismatch: baseline="
                    f"{baseline.counter_id}, current={current.counter_id}"
                ),
                baseline_counter_id=baseline.counter_id,
                current_counter_id=current.counter_id,
            )

        baseline_total = baseline.total_conversions()
        current_total = current.total_conversions()

        # (0) Empty baseline → warn, regardless of current state.
        if not baseline.goals or baseline_total == 0:
            return CheckResult.warn_result(
                "baseline has no conversions; integrity check cannot apply",
                baseline_total=baseline_total,
                current_total=current_total,
            )

        # (1) Absolute floor.
        if self._policy.min_conversions_total > 0 and (
            current_total < self._policy.min_conversions_total
        ):
            return CheckResult.blocked_result(
                (
                    f"current total conversions {current_total} is below "
                    f"minimum {self._policy.min_conversions_total}"
                ),
                current_total=current_total,
                baseline_total=baseline_total,
                min_total=self._policy.min_conversions_total,
            )

        # (2) Ratio vs baseline. Guard against divide-by-zero via the
        # empty-baseline branch above.
        ratio = current_total / baseline_total
        if self._policy.min_ratio_vs_baseline > 0 and (ratio < self._policy.min_ratio_vs_baseline):
            return CheckResult.blocked_result(
                (
                    f"current/baseline ratio {ratio:.3f} is below "
                    f"minimum {self._policy.min_ratio_vs_baseline}"
                ),
                current_total=current_total,
                baseline_total=baseline_total,
                ratio=ratio,
                min_ratio=self._policy.min_ratio_vs_baseline,
            )

        # (3) Missing goals. Only checked when the operator wants it.
        if self._policy.require_all_baseline_goals_present:
            missing = sorted(baseline.goal_ids() - current.goal_ids())
            if missing:
                return CheckResult.blocked_result(
                    (f"baseline goals missing in current snapshot: {missing}"),
                    missing_goal_ids=missing,
                )

        return CheckResult.ok_result(
            baseline_total=baseline_total,
            current_total=current_total,
        )


# --------------------------------------------------------------------------
# Kill-switch #7 — Query drift detector.
#
# Second system-level gatekeeper (alongside KS#6). Compares two sets
# of observed search queries — baseline (e.g. last week) and current
# (e.g. today) — and blocks when the share of *new* queries (present
# in current, absent in baseline) exceeds a configured fraction.
# Signals that Direct may have started showing ads to an unintended
# audience (broad-match drift, bid-strategy anomaly, etc).
#
# Matching is case- and whitespace-insensitive and Unicode-canonical
# (NFC) via the shared ``_normalize_keyword`` helper introduced for
# KS#3. Duplicate entries within a snapshot collapse to one via set
# semantics.
#
# Out of scope for this PR (BACKLOG):
# - Real Metrika / Direct API integration (§M6).
# - Reach-weighted drift (new queries with many impressions vs a
#   handful) — today the check is population-based, not impression-
#   weighted. A future refinement could require baseline/current
#   impressions per query.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SearchQueriesSnapshot:
    """Distinct search queries observed over one reporting window.

    ``queries`` is a list on the wire (YAML / API) but folds to a
    normalised set for comparison. Callers shouldn't assume the list
    is deduplicated or canonicalised; that work happens in
    ``normalised()``.
    """

    counter_id: int
    queries: list[str] = field(default_factory=list)

    def normalised(self) -> frozenset[str]:
        """Return the set of NFC / stripped / lower-cased non-empty queries."""
        out: set[str] = set()
        for q in self.queries:
            norm = _normalize_keyword(q)
            if norm:
                out.add(norm)
        return frozenset(out)


class QueryDriftPolicy(BaseModel):
    """Kill-switch #7 policy slice.

    ``max_new_query_share`` is the strict upper bound on
    ``|new_queries| / |current_queries|`` before the plan is refused.
    Default 0.4 per §M2.1 — a run where 40%+ of today's queries did
    not exist a week ago is almost always an audience-targeting
    anomaly worth a human's eyes.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_new_query_share: float = Field(
        default=0.4,
        ge=0.0,
        le=1.0,
        description=(
            "Max share of current queries that may be absent from the baseline snapshot, in [0, 1]."
        ),
    )


def load_query_drift_policy(path: Path) -> QueryDriftPolicy:
    """Read ``agent_policy.yml`` and extract the query-drift slice."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    fields = {k: raw[k] for k in ("max_new_query_share",) if k in raw}
    return QueryDriftPolicy.model_validate(fields)


class QueryDriftCheck:
    """Block plans when today's query mix drifted too far from baseline.

    Pipeline:
    0. counter_id mismatch between snapshots → blocked (same safeguard
       as KS#6; comparing different counters is meaningless).
    1. Empty baseline or empty current → warn. No reference point or
       no data to compare; pipeline can decide whether a warn aborts
       autonomous operation (M2.2).
    2. Normalise both sides (shared _normalize_keyword — NFC, strip,
       lower). Collapses case/whitespace/encoding variants that would
       otherwise inflate the drift metric.
    3. new_share = |current - baseline| / |current|. Strict `>`
       threshold blocks; equality passes so operators can set an
       exact ceiling.
    """

    _SAMPLE_LIMIT = 10

    def __init__(self, policy: QueryDriftPolicy) -> None:
        self._policy = policy

    def check(
        self,
        baseline: SearchQueriesSnapshot,
        current: SearchQueriesSnapshot,
    ) -> CheckResult:
        if baseline.counter_id != current.counter_id:
            return CheckResult.blocked_result(
                (
                    f"snapshot counter_id mismatch: baseline="
                    f"{baseline.counter_id}, current={current.counter_id}"
                ),
                baseline_counter_id=baseline.counter_id,
                current_counter_id=current.counter_id,
            )

        baseline_set = baseline.normalised()
        current_set = current.normalised()

        if not baseline_set:
            return CheckResult.warn_result(
                "baseline has no queries; drift check cannot apply",
                baseline_size=0,
                current_size=len(current_set),
            )
        if not current_set:
            return CheckResult.warn_result(
                "current has no queries; drift check cannot apply",
                baseline_size=len(baseline_set),
                current_size=0,
            )

        new_queries = current_set - baseline_set
        new_count = len(new_queries)
        current_size = len(current_set)
        new_share = new_count / current_size

        threshold = self._policy.max_new_query_share
        if new_share > threshold:
            # Surface a small sample of the offending queries so a
            # human reviewer can paste them into the search-term
            # report without re-running analysis.
            #
            # privacy-note: these are raw (normalised but not
            # redacted) user queries. Direct search terms can
            # contain names, addresses, or medical phrases. The
            # audit sink (M2.3) must hash or truncate this list
            # before log persistence. BACKLOG item tracked.
            sample = sorted(new_queries)[: self._SAMPLE_LIMIT]
            return CheckResult.blocked_result(
                (f"new-query share {new_share:.3f} exceeds threshold {threshold}"),
                new_share=new_share,
                threshold=threshold,
                current_size=current_size,
                baseline_size=len(baseline_set),
                new_count=new_count,
                new_queries_sample=sample,
            )

        return CheckResult.ok_result(
            new_share=new_share,
            baseline_size=len(baseline_set),
            current_size=current_size,
        )


# ==========================================================================
# M2.1 — Unified Policy schema.
#
# Objectives:
# - One object the pipeline runner (M2.2) can consume instead of
#   juggling seven separate load_*_policy calls.
# - Add approval tiers, per-op thresholds, forbidden-ops list, and
#   rollout_stage from §M2.1 of docs/TECHNICAL_SPEC.md. Those four
#   groups of fields have no kill-switch of their own yet — they
#   land here so the pipeline runner already has them available.
# - Keep the YAML file flat so operators don't need to restructure
#   agent_policy.yml when the full schema lands. `load_policy(path)`
#   slices the flat dict into nested sub-policies internally.
#
# Backwards compatibility: the seven individual `*Policy` classes and
# `load_*_policy` functions stay intact. Existing tests do not move.
# This PR is additive.
# ==========================================================================


RolloutStage = Literal[
    "shadow",
    "assist",
    "autonomy_light",
    "autonomy_full",
]


class Policy(BaseModel):
    """Everything the pipeline runner needs to decide on a plan.

    Nested slice-policies carry the kill-switch rules. Top-level
    fields carry approval tiers, per-op thresholds, forbidden ops,
    and the rollout stage. Every field has a defensible default
    except ``budget_cap.account_daily_budget_cap_rub`` — we refuse
    to run without an explicit account-level spend ceiling.

    ``extra="forbid"`` so a typo in agent_policy.yml becomes a loud
    ValidationError at load time, not a silent-fallback-to-default.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    # --- Kill-switch slices (§M2.0) -----------------------------------------
    budget_cap: BudgetCapPolicy
    max_cpc: MaxCpcPolicy = Field(default_factory=MaxCpcPolicy)
    negative_keyword_floor: NegativeKeywordFloorPolicy = Field(
        default_factory=NegativeKeywordFloorPolicy
    )
    quality_score_guard: QualityScoreGuardPolicy = Field(default_factory=QualityScoreGuardPolicy)
    budget_balance_drift: BudgetBalanceDriftPolicy = Field(default_factory=BudgetBalanceDriftPolicy)
    conversion_integrity: ConversionIntegrityPolicy = Field(
        default_factory=ConversionIntegrityPolicy
    )
    query_drift: QueryDriftPolicy = Field(default_factory=QueryDriftPolicy)

    # --- Approval tiers (§M2.1) ---------------------------------------------
    auto_approve_readonly: bool = True
    # pause is always reversible — safe to auto-approve.
    auto_approve_pause: bool = True
    # resume starts spending money — defaults to "no".
    auto_approve_resume: bool = False
    # adding negative keywords only reduces traffic — auto-approve.
    auto_approve_negative_keywords: bool = True

    # --- Thresholds per single operation (§M2.1) ---------------------------
    max_daily_budget_change_pct: float = Field(default=0.2, ge=0, le=1)
    max_bid_increase_pct: float = Field(default=0.5, ge=0, le=10)
    max_bid_change_per_day_pct: float = Field(default=0.25, ge=0, le=1)
    max_bulk_size: int = Field(default=50, ge=1)
    # Snapshot-freshness ceiling for apply-plan re-review: a plan
    # whose ``ReviewContext.baseline_timestamp`` is older than this
    # many seconds at apply time fails terminally and the operator
    # re-issues against fresh data. KS#1 / KS#4 both compare
    # proposed values against snapshot baselines — without this
    # bound, a parallel-operator change between plan creation and
    # apply is invisible to the guard. Default 300 s matches the
    # expected operator workflow (read plan, type command, apply)
    # while being tight enough that hours-old plans get re-checked.
    # Auditor M2-bid-snapshot / M2-ks3-negatives HIGH-2 follow-up.
    max_snapshot_age_seconds: int = Field(default=300, ge=1)

    # --- Forbidden operations (always blocked) -----------------------------
    forbidden_operations: list[str] = Field(
        default_factory=lambda: [
            "delete_campaigns",
            "delete_ads",
            "archive_campaigns_bulk",
        ]
    )

    # --- Staged rollout (§M2.5; enforcement ships with M2.5) ---------------
    rollout_stage: RolloutStage = "shadow"

    @field_validator("forbidden_operations")
    @classmethod
    def _normalise_forbidden_operations(cls, values: list[str]) -> list[str]:
        """Reject blank entries and normalise to lowercase snake_case.

        Without this, a typo in ``agent_policy.yml`` silently replaces
        the default three-entry block list with one useless entry, and
        the other two operations become permitted. Normalisation here
        means the M2.2 pipeline can do case-insensitive lookup without
        having to lowercase on every comparison.
        """
        normalised: list[str] = []
        for v in values:
            stripped = v.strip()
            if not stripped:
                msg = "forbidden_operations must not contain empty or whitespace-only entries"
                raise ValueError(msg)
            normalised.append(stripped.lower())
        return normalised


# --------------------------------------------------------------------------
# Flat-YAML → nested-Policy loader.
#
# Operators write agent_policy.yml as a single flat dict; this loader
# routes each field to its nested slice. Unknown keys raise via the
# top-level Policy.extra="forbid" rather than being silently dropped.
# --------------------------------------------------------------------------


_BUDGET_CAP_KEYS = frozenset({"account_daily_budget_cap_rub", "campaign_group_caps_rub"})
_MAX_CPC_KEYS = frozenset({"campaign_max_cpc_rub"})
_NK_FLOOR_KEYS = frozenset({"required_negative_keywords"})
_QS_GUARD_KEYS = frozenset({"min_quality_score_for_bid_increase"})
_BALANCE_DRIFT_KEYS = frozenset({"max_shift_pct_per_day"})
_CONVERSION_KEYS = frozenset(
    {
        "min_conversions_total",
        "min_ratio_vs_baseline",
        "require_all_baseline_goals_present",
    }
)
_QUERY_DRIFT_KEYS = frozenset({"max_new_query_share"})

_TOP_LEVEL_KEYS = frozenset(
    {
        "auto_approve_readonly",
        "auto_approve_pause",
        "auto_approve_resume",
        "auto_approve_negative_keywords",
        "max_daily_budget_change_pct",
        "max_bid_increase_pct",
        "max_bid_change_per_day_pct",
        "max_bulk_size",
        "max_snapshot_age_seconds",
        "forbidden_operations",
        "rollout_stage",
    }
)


def _slice(raw: dict[str, Any], keys: frozenset[str]) -> dict[str, Any]:
    return {k: raw[k] for k in keys if k in raw}


_POLICY_FILE_MAX_BYTES = 64 * 1024  # 64 KiB — policy files are tiny.


def load_policy(path: Path) -> Policy:
    """Read ``agent_policy.yml`` and build the unified ``Policy``.

    The YAML file is flat: every field lives at the top level, no
    nesting required. This loader sorts fields into their slice
    sub-policies before handing them to Pydantic.

    Unknown keys raise ``ValidationError`` — a typo must not silently
    become a default. Every kill-switch's slice has its own
    ``extra="forbid"`` as a second line of defence.

    File-size guard: ``yaml.safe_load`` blocks arbitrary code execution
    but does not bound memory; a deeply-aliased "billion laughs" file
    can expand before Pydantic ever sees it. Anything over 64 KiB is
    almost certainly an attack or a misfile — real policies are a few
    hundred bytes.
    """
    size = path.stat().st_size
    if size > _POLICY_FILE_MAX_BYTES:
        msg = f"agent_policy.yml is {size} bytes, exceeds {_POLICY_FILE_MAX_BYTES}-byte safety cap"
        raise ValueError(msg)

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    # Collect any keys we don't recognise and raise before Pydantic does
    # so the message points at the offending YAML key directly.
    known = (
        _BUDGET_CAP_KEYS
        | _MAX_CPC_KEYS
        | _NK_FLOOR_KEYS
        | _QS_GUARD_KEYS
        | _BALANCE_DRIFT_KEYS
        | _CONVERSION_KEYS
        | _QUERY_DRIFT_KEYS
        | _TOP_LEVEL_KEYS
    )
    unknown = sorted(set(raw) - known)
    if unknown:
        msg = f"unknown keys in agent_policy.yml: {unknown}"
        raise ValueError(msg)

    top_level = _slice(raw, _TOP_LEVEL_KEYS)
    return Policy(
        budget_cap=BudgetCapPolicy.model_validate(_slice(raw, _BUDGET_CAP_KEYS)),
        max_cpc=MaxCpcPolicy.model_validate(_slice(raw, _MAX_CPC_KEYS)),
        negative_keyword_floor=NegativeKeywordFloorPolicy.model_validate(
            _slice(raw, _NK_FLOOR_KEYS)
        ),
        quality_score_guard=QualityScoreGuardPolicy.model_validate(_slice(raw, _QS_GUARD_KEYS)),
        budget_balance_drift=BudgetBalanceDriftPolicy.model_validate(
            _slice(raw, _BALANCE_DRIFT_KEYS)
        ),
        conversion_integrity=ConversionIntegrityPolicy.model_validate(
            _slice(raw, _CONVERSION_KEYS)
        ),
        query_drift=QueryDriftPolicy.model_validate(_slice(raw, _QUERY_DRIFT_KEYS)),
        **top_level,
    )
