"""Tests for the safety layer.

TDD-first: this file grows with every kill-switch in the M2 roadmap.
- KS#1 (budget caps): TestBudgetCapCheck* / TestSnapshotTotals / ...
- KS#2 (max CPC):     TestMaxCpcCheck* / TestProposedBidChange* / ...
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from yadirect_agent.agent.safety import (
    AccountBidSnapshot,
    AccountBudgetSnapshot,
    BudgetBalanceDriftCheck,
    BudgetBalanceDriftPolicy,
    BudgetCapCheck,
    BudgetCapPolicy,
    BudgetChange,
    CampaignBudget,
    CheckResult,
    ConversionIntegrityCheck,
    ConversionIntegrityPolicy,
    ConversionsSnapshot,
    GoalConversions,
    KeywordSnapshot,
    MaxCpcCheck,
    MaxCpcPolicy,
    NegativeKeywordFloorCheck,
    NegativeKeywordFloorPolicy,
    Policy,
    ProposedBidChange,
    QualityScoreGuardCheck,
    QualityScoreGuardPolicy,
    QueryDriftCheck,
    QueryDriftPolicy,
    SearchQueriesSnapshot,
    load_budget_balance_drift_policy,
    load_budget_cap_policy,
    load_conversion_integrity_policy,
    load_max_cpc_policy,
    load_negative_keyword_floor_policy,
    load_policy,
    load_quality_score_guard_policy,
    load_query_drift_policy,
)

# --------------------------------------------------------------------------
# Helpers.
# --------------------------------------------------------------------------


def _campaign(
    cid: int,
    budget_rub: float,
    *,
    state: str = "ON",
    group: str | None = None,
    name: str | None = None,
) -> CampaignBudget:
    return CampaignBudget(
        id=cid,
        name=name or f"campaign-{cid}",
        daily_budget_rub=budget_rub,
        state=state,
        group=group,
    )


def _policy(
    account_cap: int = 10_000,
    group_caps: dict[str, int] | None = None,
) -> BudgetCapPolicy:
    return BudgetCapPolicy(
        account_daily_budget_cap_rub=account_cap,
        campaign_group_caps_rub=group_caps or {},
    )


# --------------------------------------------------------------------------
# CheckResult — smoke tests for the factory helpers.
# --------------------------------------------------------------------------


class TestCheckResult:
    def test_ok_result_has_ok_status_and_no_reason(self) -> None:
        r = CheckResult.ok_result()
        assert r.status == "ok"
        assert r.reason is None
        assert r.details == {}

    def test_blocked_result_carries_reason_and_details(self) -> None:
        r = CheckResult.blocked_result("too high", projected_rub=12000, cap_rub=10000)
        assert r.status == "blocked"
        assert r.reason == "too high"
        assert r.details == {"projected_rub": 12000, "cap_rub": 10000}

    def test_warn_result_does_not_block(self) -> None:
        r = CheckResult.warn_result("approaching cap")
        assert r.status == "warn"
        assert r.reason == "approaching cap"


# --------------------------------------------------------------------------
# AccountBudgetSnapshot — totals & group totals.
# --------------------------------------------------------------------------


class TestSnapshotTotals:
    def test_total_active_budget_ignores_suspended_campaigns(self) -> None:
        snapshot = AccountBudgetSnapshot(
            campaigns=[
                _campaign(1, 500, state="ON"),
                _campaign(2, 300, state="SUSPENDED"),
                _campaign(3, 200, state="OFF"),
            ]
        )
        assert snapshot.total_active_budget_rub() == 500

    def test_group_total_excludes_other_groups(self) -> None:
        snapshot = AccountBudgetSnapshot(
            campaigns=[
                _campaign(1, 500, group="brand"),
                _campaign(2, 300, group="non-brand"),
                _campaign(3, 100, group="brand"),
                _campaign(4, 700, group=None),
            ]
        )
        assert snapshot.group_active_budget_rub("brand") == 600
        assert snapshot.group_active_budget_rub("non-brand") == 300
        assert snapshot.group_active_budget_rub("does-not-exist") == 0

    def test_group_total_ignores_suspended_within_group(self) -> None:
        snapshot = AccountBudgetSnapshot(
            campaigns=[
                _campaign(1, 500, group="brand", state="ON"),
                _campaign(2, 9999, group="brand", state="SUSPENDED"),
            ]
        )
        assert snapshot.group_active_budget_rub("brand") == 500


# --------------------------------------------------------------------------
# BudgetCapPolicy — schema validation.
# --------------------------------------------------------------------------


class TestPolicyValidation:
    def test_account_cap_is_mandatory(self) -> None:
        with pytest.raises(ValidationError):
            BudgetCapPolicy.model_validate({})

    def test_negative_account_cap_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            BudgetCapPolicy.model_validate({"account_daily_budget_cap_rub": -1})

    def test_extra_fields_are_rejected(self) -> None:
        # If we ever add a typo'd key, fail loudly instead of ignoring it.
        with pytest.raises(ValidationError):
            BudgetCapPolicy.model_validate(
                {
                    "account_daily_budget_cap_rub": 10_000,
                    "not_a_real_field": 42,
                }
            )

    def test_policy_is_immutable(self) -> None:
        p = _policy()
        with pytest.raises(ValidationError):
            p.account_daily_budget_cap_rub = 99  # type: ignore[misc]


# --------------------------------------------------------------------------
# load_budget_cap_policy — YAML roundtrip.
# --------------------------------------------------------------------------


class TestLoadPolicy:
    def test_loads_minimal_policy(self, tmp_path: Path) -> None:
        path = tmp_path / "agent_policy.yml"
        path.write_text("account_daily_budget_cap_rub: 5000\n", encoding="utf-8")

        p = load_budget_cap_policy(path)
        assert p.account_daily_budget_cap_rub == 5000
        assert p.campaign_group_caps_rub == {}

    def test_loads_group_caps(self, tmp_path: Path) -> None:
        path = tmp_path / "agent_policy.yml"
        path.write_text(
            """
account_daily_budget_cap_rub: 10000
campaign_group_caps_rub:
  brand: 3000
  non-brand: 5000
""",
            encoding="utf-8",
        )

        p = load_budget_cap_policy(path)
        assert p.campaign_group_caps_rub == {"brand": 3000, "non-brand": 5000}

    def test_tolerates_unknown_top_level_keys(self, tmp_path: Path) -> None:
        # M2.1 will add more fields; a yaml with extra unrelated keys must
        # not crash this loader.
        path = tmp_path / "agent_policy.yml"
        path.write_text(
            """
account_daily_budget_cap_rub: 10000
rollout_stage: 1
max_bid_increase_pct: 0.5
""",
            encoding="utf-8",
        )

        p = load_budget_cap_policy(path)
        assert p.account_daily_budget_cap_rub == 10000


# --------------------------------------------------------------------------
# BudgetCapCheck — the core of kill-switch #1.
#
# These are the TDD-driven behaviours. Each failing test below drives the
# next step of the implementation; once all pass, the kill-switch is
# functionally complete for this milestone.
# --------------------------------------------------------------------------


class TestBudgetCapCheckHappyPath:
    def test_ok_when_no_change_would_exceed_account_cap(self) -> None:
        check = BudgetCapCheck(_policy(account_cap=10_000))
        snapshot = AccountBudgetSnapshot(campaigns=[_campaign(1, 5_000)])

        # Raise campaign 1 from 5k to 8k; still under 10k cap.
        result = check.check(snapshot, [BudgetChange(campaign_id=1, new_daily_budget_rub=8_000)])

        assert result.status == "ok"

    def test_ok_with_no_changes(self) -> None:
        # Degenerate: empty changes list — always ok (nothing moves).
        check = BudgetCapCheck(_policy(account_cap=1_000))
        snapshot = AccountBudgetSnapshot(campaigns=[_campaign(1, 500)])

        result = check.check(snapshot, [])

        assert result.status == "ok"


class TestBudgetCapCheckAccountLevel:
    def test_blocked_when_projected_total_exceeds_account_cap(self) -> None:
        check = BudgetCapCheck(_policy(account_cap=10_000))
        snapshot = AccountBudgetSnapshot(campaigns=[_campaign(1, 5_000)])

        result = check.check(snapshot, [BudgetChange(campaign_id=1, new_daily_budget_rub=12_000)])

        assert result.status == "blocked"
        assert "account" in (result.reason or "").lower()
        # Details surface the numbers — operators need to see both to decide.
        assert result.details.get("projected_rub") == 12_000
        assert result.details.get("cap_rub") == 10_000

    def test_blocked_when_resuming_a_paused_campaign_pushes_total_over(self) -> None:
        # Resume (SUSPENDED → ON) counts toward the total even without a
        # budget change — this is why kill-switch #1 has to simulate state
        # changes, not just budget numbers.
        check = BudgetCapCheck(_policy(account_cap=10_000))
        snapshot = AccountBudgetSnapshot(
            campaigns=[
                _campaign(1, 7_000, state="ON"),
                _campaign(2, 5_000, state="SUSPENDED"),
            ]
        )

        result = check.check(snapshot, [BudgetChange(campaign_id=2, new_state="ON")])

        assert result.status == "blocked"
        assert result.details.get("projected_rub") == 12_000


class TestBudgetCapCheckGroupLevel:
    def test_blocked_when_group_cap_exceeded_even_if_account_cap_is_ok(self) -> None:
        # Account cap 20k would allow 12k; group cap 3k says no.
        check = BudgetCapCheck(_policy(account_cap=20_000, group_caps={"brand": 3_000}))
        snapshot = AccountBudgetSnapshot(
            campaigns=[
                _campaign(1, 2_000, group="brand"),
                _campaign(2, 1_000, group="brand"),
                _campaign(3, 5_000, group="non-brand"),
            ]
        )

        result = check.check(
            snapshot,
            [BudgetChange(campaign_id=1, new_daily_budget_rub=4_000)],
        )

        assert result.status == "blocked"
        assert "brand" in (result.reason or "").lower()
        assert result.details.get("group") == "brand"

    def test_ok_when_group_has_no_cap_configured(self) -> None:
        # "retargeting" has no entry in group_caps — unconstrained by group.
        check = BudgetCapCheck(_policy(account_cap=20_000, group_caps={"brand": 3_000}))
        snapshot = AccountBudgetSnapshot(campaigns=[_campaign(1, 500, group="retargeting")])

        result = check.check(
            snapshot,
            [BudgetChange(campaign_id=1, new_daily_budget_rub=15_000)],
        )

        assert result.status == "ok"


class TestBudgetChangeValidation:
    """Findings from security-auditor review of the initial green commit:
    HIGH severity bypasses through malformed BudgetChange inputs. Each
    test here drives one constraint on the type.
    """

    def test_rejects_negative_daily_budget(self) -> None:
        # HIGH. Without this, an agent could submit a negative budget to
        # shrink the projected total and slip a real change under the cap.
        with pytest.raises(ValidationError):
            BudgetChange(campaign_id=1, new_daily_budget_rub=-1.0)

    def test_accepts_zero_daily_budget(self) -> None:
        # Zero is a legitimate value (pause-ish), just not negative.
        change = BudgetChange(campaign_id=1, new_daily_budget_rub=0.0)
        assert change.new_daily_budget_rub == 0.0

    def test_rejects_unknown_state_string(self) -> None:
        # MEDIUM. Must not allow "on" / "enabled" / typos that sneak past
        # the strict-equality filter in the snapshot totals.
        with pytest.raises(ValidationError):
            BudgetChange(campaign_id=1, new_state="on")

    def test_accepts_known_state_values(self) -> None:
        for state in ("ON", "OFF", "SUSPENDED", "ENDED", "CONVERTED", "ARCHIVED"):
            change = BudgetChange(campaign_id=1, new_state=state)
            assert change.new_state == state


class TestBudgetCapCheckRejectsDuplicateChanges:
    def test_blocks_duplicate_campaign_ids_in_changes_list(self) -> None:
        # HIGH. Without this, `_project` would silently keep only the last
        # BudgetChange for a given id and run the cap check against a
        # projection that mismatches what the executor will actually do.
        check = BudgetCapCheck(_policy(account_cap=10_000))
        snapshot = AccountBudgetSnapshot(campaigns=[_campaign(1, 5_000)])

        result = check.check(
            snapshot,
            [
                BudgetChange(campaign_id=1, new_daily_budget_rub=99_000),
                BudgetChange(campaign_id=1, new_state="SUSPENDED"),
            ],
        )

        assert result.status == "blocked"
        assert "duplicate" in (result.reason or "").lower()
        assert result.details.get("campaign_id") == 1


class TestBudgetCapCheckSuspendedSemantics:
    def test_ok_when_suspended_campaign_raises_its_budget_no_cap_impact(self) -> None:
        # A SUSPENDED campaign doesn't spend today, so its budget doesn't
        # count toward today's total — even if we raise it to a value
        # that would bust the cap if the campaign were on.
        check = BudgetCapCheck(_policy(account_cap=10_000))
        snapshot = AccountBudgetSnapshot(
            campaigns=[
                _campaign(1, 9_000, state="ON"),
                _campaign(2, 1_000, state="SUSPENDED"),
            ]
        )

        result = check.check(
            snapshot,
            [BudgetChange(campaign_id=2, new_daily_budget_rub=50_000)],
        )

        assert result.status == "ok"

    def test_pausing_a_campaign_frees_budget_room(self) -> None:
        # Currently at cap; pausing one campaign should let us raise
        # another over what would otherwise be blocked.
        check = BudgetCapCheck(_policy(account_cap=10_000))
        snapshot = AccountBudgetSnapshot(
            campaigns=[
                _campaign(1, 6_000, state="ON"),
                _campaign(2, 4_000, state="ON"),
            ]
        )

        result = check.check(
            snapshot,
            [
                BudgetChange(campaign_id=2, new_state="SUSPENDED"),
                BudgetChange(campaign_id=1, new_daily_budget_rub=9_500),
            ],
        )

        assert result.status == "ok"


# ==========================================================================
# Kill-switch #2 — Max CPC per campaign.
# ==========================================================================


def _keyword(
    kid: int,
    campaign_id: int,
    *,
    search: float | None = None,
    network: float | None = None,
) -> KeywordSnapshot:
    return KeywordSnapshot(
        keyword_id=kid,
        campaign_id=campaign_id,
        current_search_bid_rub=search,
        current_network_bid_rub=network,
    )


def _cpc_policy(caps: dict[int, float] | None = None) -> MaxCpcPolicy:
    return MaxCpcPolicy(campaign_max_cpc_rub=caps or {})


class TestProposedBidChangeValidation:
    """KS#1's security-auditor lessons pre-applied: negatives rejected,
    extra fields rejected, frozen instances.
    """

    def test_rejects_negative_search_bid(self) -> None:
        with pytest.raises(ValidationError):
            ProposedBidChange(keyword_id=1, new_search_bid_rub=-1.0)

    def test_rejects_negative_network_bid(self) -> None:
        with pytest.raises(ValidationError):
            ProposedBidChange(keyword_id=1, new_network_bid_rub=-0.5)

    def test_accepts_zero(self) -> None:
        change = ProposedBidChange(keyword_id=1, new_search_bid_rub=0.0)
        assert change.new_search_bid_rub == 0.0

    def test_rejects_extra_fields(self) -> None:
        with pytest.raises(ValidationError):
            ProposedBidChange.model_validate({"keyword_id": 1, "evil_flag": True})


class TestAccountBidSnapshot:
    def test_find_returns_matching_keyword(self) -> None:
        s = AccountBidSnapshot(keywords=[_keyword(1, 100), _keyword(2, 100)])
        kw = s.find(2)
        assert kw is not None
        assert kw.keyword_id == 2

    def test_find_returns_none_for_missing_keyword(self) -> None:
        s = AccountBidSnapshot(keywords=[_keyword(1, 100)])
        assert s.find(999) is None


class TestMaxCpcPolicyValidation:
    def test_empty_policy_is_valid(self) -> None:
        p = MaxCpcPolicy()
        assert p.campaign_max_cpc_rub == {}

    def test_rejects_extra_fields(self) -> None:
        with pytest.raises(ValidationError):
            MaxCpcPolicy.model_validate({"unknown": 1})

    def test_policy_is_immutable(self) -> None:
        p = _cpc_policy({100: 10.0})
        with pytest.raises(ValidationError):
            p.campaign_max_cpc_rub = {}  # type: ignore[misc]


class TestLoadMaxCpcPolicy:
    def test_loads_policy_from_yaml(self, tmp_path: Path) -> None:
        path = tmp_path / "agent_policy.yml"
        path.write_text(
            """
campaign_max_cpc_rub:
  100: 25.0
  200: 40.0
""",
            encoding="utf-8",
        )

        p = load_max_cpc_policy(path)
        assert p.campaign_max_cpc_rub == {100: 25.0, 200: 40.0}

    def test_tolerates_unknown_keys(self, tmp_path: Path) -> None:
        # Same YAML file carries keys for other kill-switches.
        path = tmp_path / "agent_policy.yml"
        path.write_text(
            """
account_daily_budget_cap_rub: 10000
campaign_max_cpc_rub:
  100: 25.0
""",
            encoding="utf-8",
        )
        p = load_max_cpc_policy(path)
        assert p.campaign_max_cpc_rub == {100: 25.0}


class TestMaxCpcCheckHappyPath:
    def test_ok_with_no_updates(self) -> None:
        check = MaxCpcCheck(_cpc_policy({100: 20.0}))
        snapshot = AccountBidSnapshot(keywords=[_keyword(1, 100)])

        result = check.check(snapshot, [])

        assert result.status == "ok"

    def test_ok_when_bid_is_below_cap(self) -> None:
        check = MaxCpcCheck(_cpc_policy({100: 20.0}))
        snapshot = AccountBidSnapshot(keywords=[_keyword(1, 100)])

        result = check.check(
            snapshot,
            [ProposedBidChange(keyword_id=1, new_search_bid_rub=15.0)],
        )

        assert result.status == "ok"

    def test_ok_when_bid_exactly_at_cap(self) -> None:
        # Equality is not "exceeding"; strict > only.
        check = MaxCpcCheck(_cpc_policy({100: 20.0}))
        snapshot = AccountBidSnapshot(keywords=[_keyword(1, 100)])

        result = check.check(
            snapshot,
            [ProposedBidChange(keyword_id=1, new_search_bid_rub=20.0)],
        )

        assert result.status == "ok"

    def test_ok_when_campaign_has_no_cap_configured(self) -> None:
        # Policy is empty — any bid passes this check.
        check = MaxCpcCheck(_cpc_policy())
        snapshot = AccountBidSnapshot(keywords=[_keyword(1, 999)])

        result = check.check(
            snapshot,
            [ProposedBidChange(keyword_id=1, new_search_bid_rub=9999.0)],
        )

        assert result.status == "ok"


class TestMaxCpcCheckBlocksBids:
    def test_blocked_when_search_bid_exceeds_cap(self) -> None:
        check = MaxCpcCheck(_cpc_policy({100: 20.0}))
        snapshot = AccountBidSnapshot(keywords=[_keyword(1, 100)])

        result = check.check(
            snapshot,
            [ProposedBidChange(keyword_id=1, new_search_bid_rub=25.0)],
        )

        assert result.status == "blocked"
        assert "search" in (result.reason or "").lower()
        assert "100" in (result.reason or "")
        assert result.details.get("keyword_id") == 1
        assert result.details.get("campaign_id") == 100
        assert result.details.get("bid_type") == "search"
        assert result.details.get("proposed_rub") == 25.0
        assert result.details.get("cap_rub") == 20.0

    def test_blocked_when_network_bid_exceeds_cap(self) -> None:
        check = MaxCpcCheck(_cpc_policy({100: 20.0}))
        snapshot = AccountBidSnapshot(keywords=[_keyword(1, 100)])

        result = check.check(
            snapshot,
            [ProposedBidChange(keyword_id=1, new_network_bid_rub=30.0)],
        )

        assert result.status == "blocked"
        assert result.details.get("bid_type") == "network"
        assert result.details.get("proposed_rub") == 30.0

    def test_blocks_on_search_when_both_bids_exceed_cap(self) -> None:
        # Security-auditor MEDIUM/LOW finding on KS#2: evaluation order
        # between search and network is an implicit contract. A refactor
        # that swaps the order would silently change which bid_type the
        # operator sees first. Pin the order: search checked before
        # network.
        check = MaxCpcCheck(_cpc_policy({100: 20.0}))
        snapshot = AccountBidSnapshot(keywords=[_keyword(1, 100)])

        result = check.check(
            snapshot,
            [
                ProposedBidChange(
                    keyword_id=1,
                    new_search_bid_rub=25.0,
                    new_network_bid_rub=30.0,
                )
            ],
        )

        assert result.status == "blocked"
        assert result.details.get("bid_type") == "search"
        assert result.details.get("proposed_rub") == 25.0


class TestMaxCpcPolicyKeyCoercion:
    """Security-auditor MEDIUM finding: the int-keyed-dict contract is
    assumed but never asserted. Pinning here so a future merged-policy
    loader (M2.1) that goes through model_validate stays safe, and
    anyone using model_construct gets caught on the first dict lookup.
    """

    def test_model_validate_coerces_string_keys_to_int(self) -> None:
        # YAML / JSON always present dict keys as strings; pydantic v2
        # coerces them to int under model_validate. This guarantees the
        # lookup at MaxCpcCheck.check (`campaign_max_cpc_rub.get(int)`)
        # finds the entry regardless of load path.
        policy = MaxCpcPolicy.model_validate({"campaign_max_cpc_rub": {"100": 20.0, "200": 40.0}})

        assert policy.campaign_max_cpc_rub == {100: 20.0, 200: 40.0}
        # And the check actually uses the coerced int key:
        assert policy.campaign_max_cpc_rub.get(100) == 20.0
        assert policy.campaign_max_cpc_rub.get("100") is None  # type: ignore[arg-type]

    def test_stops_at_first_violation(self) -> None:
        # Multiple violations — report the first; details point to it.
        check = MaxCpcCheck(_cpc_policy({100: 20.0, 200: 5.0}))
        snapshot = AccountBidSnapshot(keywords=[_keyword(1, 100), _keyword(2, 200)])

        result = check.check(
            snapshot,
            [
                ProposedBidChange(keyword_id=1, new_search_bid_rub=25.0),
                ProposedBidChange(keyword_id=2, new_search_bid_rub=100.0),
            ],
        )

        assert result.status == "blocked"
        assert result.details.get("keyword_id") == 1

    def test_blocks_duplicate_keyword_ids_in_updates(self) -> None:
        # Lesson pre-applied from KS#1 auditor review.
        check = MaxCpcCheck(_cpc_policy({100: 20.0}))
        snapshot = AccountBidSnapshot(keywords=[_keyword(1, 100)])

        result = check.check(
            snapshot,
            [
                ProposedBidChange(keyword_id=1, new_search_bid_rub=5.0),
                ProposedBidChange(keyword_id=1, new_search_bid_rub=15.0),
            ],
        )

        assert result.status == "blocked"
        assert "duplicate" in (result.reason or "").lower()
        assert result.details.get("keyword_id") == 1

    def test_silently_skips_unknown_keyword_id(self) -> None:
        # Matches KS#1 behaviour — agent sometimes proposes an id that
        # disappeared between snapshot read and check. Logged in BACKLOG
        # as tech debt (surface as warn detail when M2.3 audit lands).
        check = MaxCpcCheck(_cpc_policy({100: 20.0}))
        snapshot = AccountBidSnapshot(keywords=[_keyword(1, 100)])

        result = check.check(
            snapshot,
            [ProposedBidChange(keyword_id=999, new_search_bid_rub=999_999.0)],
        )

        assert result.status == "ok"


# ==========================================================================
# Kill-switch #3 — Negative-keyword floor.
# ==========================================================================


def _campaign_with_kw(
    cid: int,
    budget_rub: float = 500.0,
    *,
    state: str = "SUSPENDED",
    negatives: list[str] | None = None,
) -> CampaignBudget:
    """Test helper: a SUSPENDED campaign (so resume is the operation we
    want to gate) with an explicit negative-keywords set."""
    return CampaignBudget(
        id=cid,
        name=f"campaign-{cid}",
        daily_budget_rub=budget_rub,
        state=state,
        negative_keywords=frozenset(negatives or []),
    )


def _nk_policy(required: list[str] | None = None) -> NegativeKeywordFloorPolicy:
    return NegativeKeywordFloorPolicy(required_negative_keywords=required or [])


class TestNegativeKeywordFloorPolicyValidation:
    def test_empty_policy_is_valid(self) -> None:
        p = NegativeKeywordFloorPolicy()
        assert p.required_negative_keywords == []

    def test_rejects_extra_fields(self) -> None:
        with pytest.raises(ValidationError):
            NegativeKeywordFloorPolicy.model_validate({"unknown": 1})

    def test_policy_is_immutable(self) -> None:
        p = _nk_policy(["бесплатно"])
        with pytest.raises(ValidationError):
            p.required_negative_keywords = []  # type: ignore[misc]


class TestLoadNegativeKeywordFloorPolicy:
    def test_loads_from_yaml(self, tmp_path: Path) -> None:
        path = tmp_path / "agent_policy.yml"
        path.write_text(
            """
required_negative_keywords:
  - бесплатно
  - скачать
  - отзывы
""",
            encoding="utf-8",
        )

        p = load_negative_keyword_floor_policy(path)
        assert p.required_negative_keywords == ["бесплатно", "скачать", "отзывы"]

    def test_tolerates_unknown_keys(self, tmp_path: Path) -> None:
        path = tmp_path / "agent_policy.yml"
        path.write_text(
            """
account_daily_budget_cap_rub: 5000
required_negative_keywords: [бесплатно]
""",
            encoding="utf-8",
        )
        p = load_negative_keyword_floor_policy(path)
        assert p.required_negative_keywords == ["бесплатно"]


class TestNegativeKeywordFloorHappyPath:
    def test_ok_when_policy_requires_nothing(self) -> None:
        check = NegativeKeywordFloorCheck(_nk_policy([]))
        snapshot = AccountBudgetSnapshot(campaigns=[_campaign_with_kw(1, negatives=[])])

        result = check.check(snapshot, [BudgetChange(campaign_id=1, new_state="ON")])

        assert result.status == "ok"

    def test_ok_when_no_changes(self) -> None:
        check = NegativeKeywordFloorCheck(_nk_policy(["бесплатно"]))
        snapshot = AccountBudgetSnapshot(campaigns=[_campaign_with_kw(1)])

        result = check.check(snapshot, [])

        assert result.status == "ok"

    def test_ok_when_changes_do_not_resume(self) -> None:
        # A pause or a budget-only change does not trigger the floor.
        check = NegativeKeywordFloorCheck(_nk_policy(["бесплатно"]))
        snapshot = AccountBudgetSnapshot(campaigns=[_campaign_with_kw(1, state="ON", negatives=[])])

        result = check.check(
            snapshot,
            [
                BudgetChange(campaign_id=1, new_daily_budget_rub=1000),
                BudgetChange(campaign_id=1, new_state="SUSPENDED"),
            ],
        )

        # Duplicate id on 1 — this test actually trips the duplicate
        # guard. Replace with a single change.
        assert result.status == "blocked"  # documented behaviour — duplicates rejected

    def test_ok_when_budget_only_change_on_non_resume(self) -> None:
        check = NegativeKeywordFloorCheck(_nk_policy(["бесплатно"]))
        snapshot = AccountBudgetSnapshot(campaigns=[_campaign_with_kw(1, state="ON", negatives=[])])

        # A running campaign without the required negatives that is
        # merely getting its budget changed: not KS#3's concern.
        result = check.check(
            snapshot,
            [BudgetChange(campaign_id=1, new_daily_budget_rub=1000)],
        )

        assert result.status == "ok"

    def test_ok_when_resume_target_has_all_required_negatives(self) -> None:
        check = NegativeKeywordFloorCheck(_nk_policy(["бесплатно", "скачать"]))
        snapshot = AccountBudgetSnapshot(
            campaigns=[_campaign_with_kw(1, negatives=["бесплатно", "скачать", "отзывы"])]
        )

        result = check.check(snapshot, [BudgetChange(campaign_id=1, new_state="ON")])

        assert result.status == "ok"

    def test_case_insensitive_matching(self) -> None:
        # Policy says lowercase; campaign stores uppercase variant.
        check = NegativeKeywordFloorCheck(_nk_policy(["бесплатно"]))
        snapshot = AccountBudgetSnapshot(campaigns=[_campaign_with_kw(1, negatives=["БЕСПЛАТНО"])])

        result = check.check(snapshot, [BudgetChange(campaign_id=1, new_state="ON")])

        assert result.status == "ok"

    def test_whitespace_insensitive_matching(self) -> None:
        check = NegativeKeywordFloorCheck(_nk_policy(["бесплатно"]))
        snapshot = AccountBudgetSnapshot(
            campaigns=[_campaign_with_kw(1, negatives=["  бесплатно  "])]
        )

        result = check.check(snapshot, [BudgetChange(campaign_id=1, new_state="ON")])

        assert result.status == "ok"

    def test_multi_word_negative_keyword_internal_whitespace_collapses(self) -> None:
        # Cross-kill-switch regression guard (auditor MEDIUM on KS#7):
        # `_normalize_keyword` grew an internal-whitespace collapse
        # when KS#7 landed. This test pins that KS#3 now treats a
        # double-space variant of a multi-word phrase as the same
        # phrase. Policy "бесплатно скачать" (single space) matches
        # campaign "бесплатно  скачать" (double space).
        check = NegativeKeywordFloorCheck(_nk_policy(["бесплатно скачать"]))
        snapshot = AccountBudgetSnapshot(
            campaigns=[_campaign_with_kw(1, negatives=["бесплатно  скачать"])]
        )

        result = check.check(snapshot, [BudgetChange(campaign_id=1, new_state="ON")])

        assert result.status == "ok"


class TestNegativeKeywordFloorBlocks:
    def test_blocked_when_resume_target_missing_required_negative(self) -> None:
        check = NegativeKeywordFloorCheck(_nk_policy(["бесплатно", "скачать"]))
        snapshot = AccountBudgetSnapshot(campaigns=[_campaign_with_kw(1, negatives=["бесплатно"])])

        result = check.check(snapshot, [BudgetChange(campaign_id=1, new_state="ON")])

        assert result.status == "blocked"
        assert result.details.get("campaign_id") == 1
        assert (
            "скачать" in (result.reason or "").lower()
            or "скачать" in str(result.details.get("missing", "")).lower()
        )

    def test_blocked_when_typo_prevents_match(self) -> None:
        # "беспалтно" (typo) != "бесплатно".
        check = NegativeKeywordFloorCheck(_nk_policy(["бесплатно"]))
        snapshot = AccountBudgetSnapshot(campaigns=[_campaign_with_kw(1, negatives=["беспалтно"])])

        result = check.check(snapshot, [BudgetChange(campaign_id=1, new_state="ON")])

        assert result.status == "blocked"

    def test_blocked_on_resume_even_if_other_change_is_benign(self) -> None:
        # A batch with one benign change (budget) and one resume on a
        # non-compliant campaign: the resume still gets blocked.
        check = NegativeKeywordFloorCheck(_nk_policy(["бесплатно"]))
        snapshot = AccountBudgetSnapshot(
            campaigns=[
                _campaign_with_kw(1, state="ON", negatives=["бесплатно"]),
                _campaign_with_kw(2, negatives=[]),
            ]
        )

        result = check.check(
            snapshot,
            [
                BudgetChange(campaign_id=1, new_daily_budget_rub=500),
                BudgetChange(campaign_id=2, new_state="ON"),
            ],
        )

        assert result.status == "blocked"
        assert result.details.get("campaign_id") == 2

    def test_first_violation_reported(self) -> None:
        check = NegativeKeywordFloorCheck(_nk_policy(["бесплатно"]))
        snapshot = AccountBudgetSnapshot(
            campaigns=[
                _campaign_with_kw(1, negatives=[]),
                _campaign_with_kw(2, negatives=[]),
            ]
        )

        result = check.check(
            snapshot,
            [
                BudgetChange(campaign_id=1, new_state="ON"),
                BudgetChange(campaign_id=2, new_state="ON"),
            ],
        )

        assert result.status == "blocked"
        assert result.details.get("campaign_id") == 1

    def test_blocks_duplicate_campaign_ids_in_changes(self) -> None:
        # Same auditor-driven guard as KS#1/KS#2.
        check = NegativeKeywordFloorCheck(_nk_policy([]))
        snapshot = AccountBudgetSnapshot(campaigns=[_campaign_with_kw(1)])

        result = check.check(
            snapshot,
            [
                BudgetChange(campaign_id=1, new_state="ON"),
                BudgetChange(campaign_id=1, new_daily_budget_rub=500),
            ],
        )

        assert result.status == "blocked"
        assert "duplicate" in (result.reason or "").lower()

    def test_silently_skips_unknown_campaign_id(self) -> None:
        # Consistent with KS#1/KS#2 behaviour; tech debt is in BACKLOG.
        check = NegativeKeywordFloorCheck(_nk_policy(["бесплатно"]))
        snapshot = AccountBudgetSnapshot(campaigns=[_campaign_with_kw(1, negatives=["бесплатно"])])

        result = check.check(
            snapshot,
            [BudgetChange(campaign_id=999, new_state="ON")],
        )

        assert result.status == "ok"


class TestNegativeKeywordFloorAuditorFindings:
    """Security-auditor findings on KS#3 (post-GREEN review):
    HIGH — NFC/NFD normalisation gap
    MEDIUM — empty/whitespace-only policy entries degrade to
             "block everything" denial-of-service on the safety gate
    """

    def test_matches_nfc_policy_against_nfd_campaign_keyword(self) -> None:
        # HIGH. If the Yandex Direct API (or any HTTP stack in the
        # chain) returns NFD-decomposed Cyrillic, codepoint-exact
        # comparison fails against the NFC-canonical policy string.
        # _normalize_keyword must fold both sides into NFC.
        #
        # "майка" has 'й' which decomposes to 'и' + U+0306 (combining
        # breve) under NFD — a concrete case where NFC and NFD differ.
        import unicodedata

        phrase = "майка"
        nfc = unicodedata.normalize("NFC", phrase)
        nfd = unicodedata.normalize("NFD", phrase)
        assert nfc != nfd  # precondition: the two forms differ bit-wise

        check = NegativeKeywordFloorCheck(_nk_policy([nfc]))
        snapshot = AccountBudgetSnapshot(campaigns=[_campaign_with_kw(1, negatives=[nfd])])

        result = check.check(snapshot, [BudgetChange(campaign_id=1, new_state="ON")])

        assert result.status == "ok"

    def test_matches_nfd_policy_against_nfc_campaign_keyword(self) -> None:
        # Symmetric — the fold must be bidirectional, not policy-privileged.
        import unicodedata

        phrase = "майка"
        nfc = unicodedata.normalize("NFC", phrase)
        nfd = unicodedata.normalize("NFD", phrase)

        check = NegativeKeywordFloorCheck(_nk_policy([nfd]))
        snapshot = AccountBudgetSnapshot(campaigns=[_campaign_with_kw(1, negatives=[nfc])])

        result = check.check(snapshot, [BudgetChange(campaign_id=1, new_state="ON")])

        assert result.status == "ok"

    def test_policy_rejects_empty_string_entry(self) -> None:
        # MEDIUM. A `""` in required_negative_keywords would match no
        # real campaign, effectively disabling every resume — a
        # denial-of-service on the safety gate and social pressure
        # to turn KS#3 off.
        with pytest.raises(ValidationError):
            NegativeKeywordFloorPolicy(required_negative_keywords=[""])

    def test_policy_rejects_whitespace_only_entry(self) -> None:
        with pytest.raises(ValidationError):
            NegativeKeywordFloorPolicy(required_negative_keywords=["   "])

    def test_policy_rejects_entry_that_is_empty_after_stripping(self) -> None:
        # Tabs + spaces only — same problem.
        with pytest.raises(ValidationError):
            NegativeKeywordFloorPolicy(required_negative_keywords=["\t \t"])


# ==========================================================================
# Kill-switch #4 — Quality Score guardrail.
# ==========================================================================


def _kw_with_qs(
    kid: int,
    *,
    campaign_id: int = 100,
    qs: int | None = 5,
    search: float | None = 10.0,
    network: float | None = None,
) -> KeywordSnapshot:
    """Test helper: a keyword with a QS and a current bid set."""
    return KeywordSnapshot(
        keyword_id=kid,
        campaign_id=campaign_id,
        current_search_bid_rub=search,
        current_network_bid_rub=network,
        quality_score=qs,
    )


def _qs_policy(threshold: int = 5) -> QualityScoreGuardPolicy:
    return QualityScoreGuardPolicy(min_quality_score_for_bid_increase=threshold)


class TestQualityScoreGuardPolicyValidation:
    def test_default_threshold_is_five(self) -> None:
        # Per §M2.1 default — QS 5 is the tipping point where Direct
        # starts seriously penalising CPC, so it's the de-facto floor.
        p = QualityScoreGuardPolicy()
        assert p.min_quality_score_for_bid_increase == 5

    def test_accepts_zero_threshold(self) -> None:
        # Zero means the kill-switch is practically disabled but
        # intentional — policy may want to route through other guards.
        p = _qs_policy(0)
        assert p.min_quality_score_for_bid_increase == 0

    def test_accepts_max_threshold_ten(self) -> None:
        p = _qs_policy(10)
        assert p.min_quality_score_for_bid_increase == 10

    def test_rejects_above_ten(self) -> None:
        # Direct QS range is 0-10; 11 is malformed.
        with pytest.raises(ValidationError):
            _qs_policy(11)

    def test_rejects_negative(self) -> None:
        with pytest.raises(ValidationError):
            _qs_policy(-1)

    def test_rejects_extra_fields(self) -> None:
        with pytest.raises(ValidationError):
            QualityScoreGuardPolicy.model_validate({"unknown": 1})

    def test_policy_is_immutable(self) -> None:
        p = _qs_policy(5)
        with pytest.raises(ValidationError):
            p.min_quality_score_for_bid_increase = 9  # type: ignore[misc]


class TestLoadQualityScoreGuardPolicy:
    def test_loads_from_yaml(self, tmp_path: Path) -> None:
        path = tmp_path / "agent_policy.yml"
        path.write_text("min_quality_score_for_bid_increase: 6\n", encoding="utf-8")
        p = load_quality_score_guard_policy(path)
        assert p.min_quality_score_for_bid_increase == 6

    def test_default_when_key_missing(self, tmp_path: Path) -> None:
        # Missing key → default (5).
        path = tmp_path / "agent_policy.yml"
        path.write_text("account_daily_budget_cap_rub: 10000\n", encoding="utf-8")
        p = load_quality_score_guard_policy(path)
        assert p.min_quality_score_for_bid_increase == 5


class TestQualityScoreGuardCheckHappyPath:
    def test_ok_with_no_updates(self) -> None:
        check = QualityScoreGuardCheck(_qs_policy(5))
        snapshot = AccountBidSnapshot(keywords=[_kw_with_qs(1, qs=3)])

        result = check.check(snapshot, [])

        assert result.status == "ok"

    def test_ok_when_bid_is_not_increasing(self) -> None:
        # Same or lower bid — kill-switch #4 never blocks a decrease,
        # regardless of QS.
        check = QualityScoreGuardCheck(_qs_policy(5))
        snapshot = AccountBidSnapshot(keywords=[_kw_with_qs(1, qs=2, search=10.0)])

        result = check.check(
            snapshot,
            [ProposedBidChange(keyword_id=1, new_search_bid_rub=8.0)],
        )

        assert result.status == "ok"

    def test_ok_when_bid_is_increasing_and_qs_meets_threshold(self) -> None:
        check = QualityScoreGuardCheck(_qs_policy(5))
        snapshot = AccountBidSnapshot(keywords=[_kw_with_qs(1, qs=7, search=10.0)])

        result = check.check(
            snapshot,
            [ProposedBidChange(keyword_id=1, new_search_bid_rub=15.0)],
        )

        assert result.status == "ok"

    def test_ok_when_qs_exactly_at_threshold(self) -> None:
        # Strict `<` blocks; equality is ok.
        check = QualityScoreGuardCheck(_qs_policy(5))
        snapshot = AccountBidSnapshot(keywords=[_kw_with_qs(1, qs=5, search=10.0)])

        result = check.check(
            snapshot,
            [ProposedBidChange(keyword_id=1, new_search_bid_rub=15.0)],
        )

        assert result.status == "ok"

    def test_ok_when_quality_score_is_unknown(self) -> None:
        # QS=None means Direct hasn't scored this keyword yet (new
        # keyword). A missing signal is not evidence of a bad signal;
        # KS#4 defers. Agent should set QS before retrying.
        check = QualityScoreGuardCheck(_qs_policy(5))
        snapshot = AccountBidSnapshot(keywords=[_kw_with_qs(1, qs=None, search=10.0)])

        result = check.check(
            snapshot,
            [ProposedBidChange(keyword_id=1, new_search_bid_rub=15.0)],
        )

        assert result.status == "ok"

    def test_ok_when_current_bid_is_unknown(self) -> None:
        # current_search_bid_rub=None → we can't judge "increasing".
        # Don't block by default; agent should read bids first.
        check = QualityScoreGuardCheck(_qs_policy(5))
        snapshot = AccountBidSnapshot(keywords=[_kw_with_qs(1, qs=2, search=None, network=None)])

        result = check.check(
            snapshot,
            [ProposedBidChange(keyword_id=1, new_search_bid_rub=15.0)],
        )

        assert result.status == "ok"


class TestQualityScoreGuardCheckBlocks:
    def test_blocked_when_raising_search_bid_with_low_qs(self) -> None:
        check = QualityScoreGuardCheck(_qs_policy(5))
        snapshot = AccountBidSnapshot(keywords=[_kw_with_qs(1, qs=3, search=10.0)])

        result = check.check(
            snapshot,
            [ProposedBidChange(keyword_id=1, new_search_bid_rub=15.0)],
        )

        assert result.status == "blocked"
        assert result.details.get("keyword_id") == 1
        assert result.details.get("quality_score") == 3
        assert result.details.get("threshold") == 5
        assert result.details.get("bid_type") == "search"

    def test_blocked_when_raising_network_bid_with_low_qs(self) -> None:
        check = QualityScoreGuardCheck(_qs_policy(5))
        snapshot = AccountBidSnapshot(keywords=[_kw_with_qs(1, qs=3, search=None, network=5.0)])

        result = check.check(
            snapshot,
            [ProposedBidChange(keyword_id=1, new_network_bid_rub=8.0)],
        )

        assert result.status == "blocked"
        assert result.details.get("bid_type") == "network"
        assert result.details.get("quality_score") == 3

    def test_blocks_on_search_when_both_fields_raise_low_qs(self) -> None:
        # Pin search-before-network evaluation order (matches KS#2).
        check = QualityScoreGuardCheck(_qs_policy(5))
        snapshot = AccountBidSnapshot(keywords=[_kw_with_qs(1, qs=2, search=10.0, network=5.0)])

        result = check.check(
            snapshot,
            [
                ProposedBidChange(
                    keyword_id=1,
                    new_search_bid_rub=15.0,
                    new_network_bid_rub=8.0,
                )
            ],
        )

        assert result.status == "blocked"
        assert result.details.get("bid_type") == "search"

    def test_first_violation_wins(self) -> None:
        check = QualityScoreGuardCheck(_qs_policy(5))
        snapshot = AccountBidSnapshot(
            keywords=[
                _kw_with_qs(1, qs=8, search=10.0),
                _kw_with_qs(2, qs=2, search=10.0),
            ]
        )

        result = check.check(
            snapshot,
            [
                ProposedBidChange(keyword_id=1, new_search_bid_rub=15.0),
                ProposedBidChange(keyword_id=2, new_search_bid_rub=15.0),
            ],
        )

        assert result.status == "blocked"
        assert result.details.get("keyword_id") == 2

    def test_blocks_duplicate_keyword_ids_in_updates(self) -> None:
        # Shared guard — already in the helper.
        check = QualityScoreGuardCheck(_qs_policy(5))
        snapshot = AccountBidSnapshot(keywords=[_kw_with_qs(1, qs=8)])

        result = check.check(
            snapshot,
            [
                ProposedBidChange(keyword_id=1, new_search_bid_rub=15.0),
                ProposedBidChange(keyword_id=1, new_network_bid_rub=8.0),
            ],
        )

        assert result.status == "blocked"
        assert "duplicate" in (result.reason or "").lower()

    def test_silently_skips_unknown_keyword_id(self) -> None:
        check = QualityScoreGuardCheck(_qs_policy(5))
        snapshot = AccountBidSnapshot(keywords=[_kw_with_qs(1, qs=8)])

        result = check.check(
            snapshot,
            [ProposedBidChange(keyword_id=999, new_search_bid_rub=15.0)],
        )

        assert result.status == "ok"

    def test_does_not_block_lowering_bid_on_low_qs_keyword(self) -> None:
        # Edge: QS is below threshold but the agent is *lowering* the
        # bid. This is exactly what an operator would want (save money
        # on a low-QS keyword). Must not block.
        check = QualityScoreGuardCheck(_qs_policy(5))
        snapshot = AccountBidSnapshot(keywords=[_kw_with_qs(1, qs=2, search=10.0)])

        result = check.check(
            snapshot,
            [ProposedBidChange(keyword_id=1, new_search_bid_rub=5.0)],
        )

        assert result.status == "ok"

    def test_does_not_block_equal_bid_on_low_qs_keyword(self) -> None:
        # Same-value update is a no-op, not an increase.
        check = QualityScoreGuardCheck(_qs_policy(5))
        snapshot = AccountBidSnapshot(keywords=[_kw_with_qs(1, qs=2, search=10.0)])

        result = check.check(
            snapshot,
            [ProposedBidChange(keyword_id=1, new_search_bid_rub=10.0)],
        )

        assert result.status == "ok"


class TestKeywordSnapshotQualityScoreTypeContract:
    """Security-auditor LOW finding on KS#4: `KeywordSnapshot` is a
    plain frozen dataclass, so Python performs zero runtime validation
    on field types. Without this, a caller could slip
    `quality_score=4.5` past us and the `>= threshold` comparison
    would silently mis-classify borderline keywords.

    These tests pin the `__post_init__` type contract.
    """

    def test_rejects_float_quality_score(self) -> None:
        with pytest.raises(TypeError, match="quality_score"):
            KeywordSnapshot(keyword_id=1, campaign_id=100, quality_score=4.5)  # type: ignore[arg-type]

    def test_rejects_string_quality_score(self) -> None:
        with pytest.raises(TypeError, match="quality_score"):
            KeywordSnapshot(keyword_id=1, campaign_id=100, quality_score="5")  # type: ignore[arg-type]

    def test_rejects_bool_quality_score(self) -> None:
        # bool is a subclass of int in Python; we reject it explicitly
        # so True/False do not silently become QS 1/0.
        with pytest.raises(TypeError, match="quality_score"):
            KeywordSnapshot(keyword_id=1, campaign_id=100, quality_score=True)  # type: ignore[arg-type]

    def test_rejects_out_of_range_quality_score(self) -> None:
        with pytest.raises(ValueError, match=r"range 0\.\.10"):
            KeywordSnapshot(keyword_id=1, campaign_id=100, quality_score=11)
        with pytest.raises(ValueError, match=r"range 0\.\.10"):
            KeywordSnapshot(keyword_id=1, campaign_id=100, quality_score=-1)

    def test_accepts_boundary_values(self) -> None:
        for qs in (0, 5, 10, None):
            kw = KeywordSnapshot(keyword_id=1, campaign_id=100, quality_score=qs)
            assert kw.quality_score == qs


# ==========================================================================
# Kill-switch #5 — Budget-balance drift.
# ==========================================================================


def _ab_campaign(
    cid: int,
    budget_rub: float,
    *,
    state: str = "ON",
) -> CampaignBudget:
    """Test helper: a campaign with just the fields KS#5 reads."""
    return CampaignBudget(
        id=cid,
        name=f"c-{cid}",
        daily_budget_rub=budget_rub,
        state=state,
    )


def _bbd_policy(max_shift: float = 0.3) -> BudgetBalanceDriftPolicy:
    return BudgetBalanceDriftPolicy(max_shift_pct_per_day=max_shift)


class TestBudgetBalanceDriftPolicyValidation:
    def test_default_is_30_percent(self) -> None:
        p = BudgetBalanceDriftPolicy()
        assert p.max_shift_pct_per_day == 0.3

    def test_accepts_fraction_in_range(self) -> None:
        for v in (0.01, 0.5, 1.0):
            assert _bbd_policy(v).max_shift_pct_per_day == v

    def test_rejects_zero_or_below(self) -> None:
        # Zero means "no shift allowed ever" — arguably valid but almost
        # certainly a typo; we require gt=0 so the operator is forced to
        # be explicit (disable the kill-switch at the pipeline level
        # instead of setting a degenerate policy).
        with pytest.raises(ValidationError):
            _bbd_policy(0.0)
        with pytest.raises(ValidationError):
            _bbd_policy(-0.1)

    def test_rejects_above_one(self) -> None:
        with pytest.raises(ValidationError):
            _bbd_policy(1.5)

    def test_rejects_extra_fields(self) -> None:
        with pytest.raises(ValidationError):
            BudgetBalanceDriftPolicy.model_validate({"unknown": 1})

    def test_policy_is_immutable(self) -> None:
        p = _bbd_policy()
        with pytest.raises(ValidationError):
            p.max_shift_pct_per_day = 0.5  # type: ignore[misc]


class TestLoadBudgetBalanceDriftPolicy:
    def test_loads_from_yaml(self, tmp_path: Path) -> None:
        path = tmp_path / "agent_policy.yml"
        path.write_text("max_shift_pct_per_day: 0.2\n", encoding="utf-8")
        p = load_budget_balance_drift_policy(path)
        assert p.max_shift_pct_per_day == 0.2

    def test_default_when_key_missing(self, tmp_path: Path) -> None:
        path = tmp_path / "agent_policy.yml"
        path.write_text("account_daily_budget_cap_rub: 10000\n", encoding="utf-8")
        p = load_budget_balance_drift_policy(path)
        assert p.max_shift_pct_per_day == 0.3


class TestBudgetBalanceDriftHappyPath:
    def test_ok_with_no_changes(self) -> None:
        # Empty changes — nothing moves; projected == snapshot.
        check = BudgetBalanceDriftCheck(_bbd_policy(0.3))
        baseline = AccountBudgetSnapshot(campaigns=[_ab_campaign(1, 500), _ab_campaign(2, 500)])
        snapshot = AccountBudgetSnapshot(campaigns=[_ab_campaign(1, 500), _ab_campaign(2, 500)])

        result = check.check(baseline, snapshot, [])

        assert result.status == "ok"

    def test_ok_when_shift_under_threshold(self) -> None:
        # Baseline: 50/50. Propose 70/30 — each campaign's share shifts
        # by 20pp, which is strictly below the 30% threshold.
        check = BudgetBalanceDriftCheck(_bbd_policy(0.3))
        baseline = AccountBudgetSnapshot(campaigns=[_ab_campaign(1, 500), _ab_campaign(2, 500)])
        snapshot = AccountBudgetSnapshot(campaigns=[_ab_campaign(1, 500), _ab_campaign(2, 500)])
        # Post-change: campaign 1 → 700, campaign 2 stays 300 (via budget
        # change on campaign 2).
        result = check.check(
            baseline,
            snapshot,
            [
                BudgetChange(campaign_id=1, new_daily_budget_rub=700),
                BudgetChange(campaign_id=2, new_daily_budget_rub=300),
            ],
        )

        assert result.status == "ok"

    def test_ok_when_shift_exactly_at_threshold(self) -> None:
        # Strict `>` blocks; equality is ok.
        check = BudgetBalanceDriftCheck(_bbd_policy(0.3))
        baseline = AccountBudgetSnapshot(campaigns=[_ab_campaign(1, 500), _ab_campaign(2, 500)])
        snapshot = AccountBudgetSnapshot(campaigns=[_ab_campaign(1, 500), _ab_campaign(2, 500)])
        # Shift 1 → 800 (80%), 2 → 200 (20%). Each shifts exactly 30pp.
        result = check.check(
            baseline,
            snapshot,
            [
                BudgetChange(campaign_id=1, new_daily_budget_rub=800),
                BudgetChange(campaign_id=2, new_daily_budget_rub=200),
            ],
        )

        assert result.status == "ok"


class TestBudgetBalanceDriftBlocks:
    def test_blocked_when_one_campaign_absorbs_everything(self) -> None:
        # The core "agent drained everything into one campaign" case.
        # Baseline 50/50 → projected 95/5.
        check = BudgetBalanceDriftCheck(_bbd_policy(0.3))
        baseline = AccountBudgetSnapshot(campaigns=[_ab_campaign(1, 500), _ab_campaign(2, 500)])
        snapshot = AccountBudgetSnapshot(campaigns=[_ab_campaign(1, 500), _ab_campaign(2, 500)])
        result = check.check(
            baseline,
            snapshot,
            [
                BudgetChange(campaign_id=1, new_daily_budget_rub=950),
                BudgetChange(campaign_id=2, new_daily_budget_rub=50),
            ],
        )

        assert result.status == "blocked"
        # One campaign shifted; details name it and its drift.
        assert result.details.get("campaign_id") in {1, 2}
        assert result.details.get("threshold") == 0.3
        # drift should be ~0.45 (|0.95 - 0.5|)
        drift = result.details.get("shift_pct", 0.0)
        assert drift > 0.3

    def test_blocked_when_resuming_a_large_paused_campaign(self) -> None:
        # Baseline: only campaign 1 active at 100% share.
        # Current: campaign 2 also exists but SUSPENDED. Propose to resume
        # campaign 2 with equal budget — campaign 1's share drops from
        # 100% to 50% (50pp drift), which busts 30%.
        check = BudgetBalanceDriftCheck(_bbd_policy(0.3))
        baseline = AccountBudgetSnapshot(campaigns=[_ab_campaign(1, 500)])
        snapshot = AccountBudgetSnapshot(
            campaigns=[_ab_campaign(1, 500), _ab_campaign(2, 500, state="SUSPENDED")]
        )
        result = check.check(
            baseline,
            snapshot,
            [BudgetChange(campaign_id=2, new_state="ON")],
        )

        assert result.status == "blocked"

    def test_blocked_when_pausing_a_large_campaign(self) -> None:
        # Baseline: 1 has 50% share. Pause → 1's share becomes 0%.
        # |0 - 0.5| = 0.5 drift > threshold 0.3.
        check = BudgetBalanceDriftCheck(_bbd_policy(0.3))
        baseline = AccountBudgetSnapshot(campaigns=[_ab_campaign(1, 500), _ab_campaign(2, 500)])
        snapshot = AccountBudgetSnapshot(campaigns=[_ab_campaign(1, 500), _ab_campaign(2, 500)])
        result = check.check(
            baseline,
            snapshot,
            [BudgetChange(campaign_id=1, new_state="SUSPENDED")],
        )

        assert result.status == "blocked"

    def test_first_drifting_campaign_wins(self) -> None:
        # Two campaigns both drift; first one returned in details.
        check = BudgetBalanceDriftCheck(_bbd_policy(0.3))
        baseline = AccountBudgetSnapshot(
            campaigns=[
                _ab_campaign(1, 100),
                _ab_campaign(2, 100),
                _ab_campaign(3, 100),
            ]
        )
        snapshot = AccountBudgetSnapshot(
            campaigns=[
                _ab_campaign(1, 100),
                _ab_campaign(2, 100),
                _ab_campaign(3, 100),
            ]
        )
        # Bring campaign 1 up to 800 (share .8), campaign 2 down to 10
        # (share .01), campaign 3 stays. Both campaigns 1 and 2 drift
        # over 30pp — only one is reported.
        result = check.check(
            baseline,
            snapshot,
            [
                BudgetChange(campaign_id=1, new_daily_budget_rub=800),
                BudgetChange(campaign_id=2, new_daily_budget_rub=10),
            ],
        )

        assert result.status == "blocked"


class TestBudgetBalanceDriftEdgeCases:
    def test_warns_when_baseline_is_empty(self) -> None:
        # First-ever agent run: no baseline. Silent ok would let a
        # compromised bootstrap run one unchecked rebalance — the
        # security-auditor LOW on KS#5 called this out. We emit
        # `warn` so the M2.3 audit sink surfaces the skipped check,
        # and the M2.2 pipeline layer can refuse autonomous
        # operation until a real baseline exists.
        check = BudgetBalanceDriftCheck(_bbd_policy(0.3))
        baseline = AccountBudgetSnapshot(campaigns=[])
        snapshot = AccountBudgetSnapshot(campaigns=[_ab_campaign(1, 1000), _ab_campaign(2, 1000)])

        result = check.check(
            baseline,
            snapshot,
            [BudgetChange(campaign_id=1, new_daily_budget_rub=5000)],
        )

        assert result.status == "warn"
        assert "baseline" in (result.reason or "").lower()
        # Pipeline orchestrator can read these to refuse autonomous
        # execution on empty baselines.
        assert result.details.get("baseline_total_rub") == 0

    def test_ok_when_projected_total_is_zero(self) -> None:
        # Every active campaign gets paused — nothing spending, drift
        # metric is undefined and irrelevant. Not our problem.
        check = BudgetBalanceDriftCheck(_bbd_policy(0.3))
        baseline = AccountBudgetSnapshot(campaigns=[_ab_campaign(1, 500), _ab_campaign(2, 500)])
        snapshot = AccountBudgetSnapshot(campaigns=[_ab_campaign(1, 500), _ab_campaign(2, 500)])
        result = check.check(
            baseline,
            snapshot,
            [
                BudgetChange(campaign_id=1, new_state="SUSPENDED"),
                BudgetChange(campaign_id=2, new_state="SUSPENDED"),
            ],
        )

        assert result.status == "ok"

    def test_warns_when_baseline_total_is_zero(self) -> None:
        # Same-class case as empty baseline: nobody was active yesterday.
        # Safer to warn than silently allow any rebalance.
        check = BudgetBalanceDriftCheck(_bbd_policy(0.3))
        baseline = AccountBudgetSnapshot(campaigns=[_ab_campaign(1, 500, state="SUSPENDED")])
        snapshot = AccountBudgetSnapshot(campaigns=[_ab_campaign(1, 500), _ab_campaign(2, 500)])
        result = check.check(
            baseline,
            snapshot,
            [BudgetChange(campaign_id=1, new_state="ON")],
        )

        assert result.status == "warn"

    def test_blocks_duplicate_campaign_ids_in_changes(self) -> None:
        check = BudgetBalanceDriftCheck(_bbd_policy(0.3))
        baseline = AccountBudgetSnapshot(campaigns=[_ab_campaign(1, 500)])
        snapshot = AccountBudgetSnapshot(campaigns=[_ab_campaign(1, 500)])

        result = check.check(
            baseline,
            snapshot,
            [
                BudgetChange(campaign_id=1, new_daily_budget_rub=500),
                BudgetChange(campaign_id=1, new_state="SUSPENDED"),
            ],
        )

        assert result.status == "blocked"
        assert "duplicate" in (result.reason or "").lower()

    def test_campaign_only_in_snapshot_counts_as_zero_baseline_share(self) -> None:
        # A campaign that appeared today (not in yesterday's baseline)
        # starts at 0% baseline share. If it takes a significant share
        # of the projected distribution, that's > threshold drift and
        # must be blocked.
        check = BudgetBalanceDriftCheck(_bbd_policy(0.3))
        baseline = AccountBudgetSnapshot(campaigns=[_ab_campaign(1, 500)])
        snapshot = AccountBudgetSnapshot(campaigns=[_ab_campaign(1, 500), _ab_campaign(2, 500)])
        # Campaign 2 has 0% baseline, ~50% projected → 50pp drift.
        result = check.check(baseline, snapshot, [])

        assert result.status == "blocked"

    def test_campaign_only_in_baseline_counts_as_zero_projected_share(self) -> None:
        # A campaign archived between days: baseline share N%, projected
        # share 0%. Treat drift as that N%, block if over threshold.
        check = BudgetBalanceDriftCheck(_bbd_policy(0.3))
        baseline = AccountBudgetSnapshot(campaigns=[_ab_campaign(1, 500), _ab_campaign(2, 500)])
        snapshot = AccountBudgetSnapshot(campaigns=[_ab_campaign(1, 500)])

        result = check.check(baseline, snapshot, [])

        # Campaign 2 went from 50% → 0% (50pp drop).
        assert result.status == "blocked"


# ==========================================================================
# Kill-switch #6 — Conversion integrity.
# ==========================================================================


def _goals(*specs: tuple[int, int]) -> list[GoalConversions]:
    """Helper: [(goal_id, conversions), ...] → list[GoalConversions]."""
    return [
        GoalConversions(goal_id=gid, goal_name=f"goal-{gid}", conversions=n) for gid, n in specs
    ]


def _snap(goals: list[GoalConversions], *, counter_id: int = 1) -> ConversionsSnapshot:
    return ConversionsSnapshot(counter_id=counter_id, goals=goals)


def _ci_policy(
    *,
    min_total: int = 1,
    min_ratio: float = 0.5,
    require_goals: bool = True,
) -> ConversionIntegrityPolicy:
    return ConversionIntegrityPolicy(
        min_conversions_total=min_total,
        min_ratio_vs_baseline=min_ratio,
        require_all_baseline_goals_present=require_goals,
    )


class TestConversionIntegrityPolicyValidation:
    def test_default_policy_is_sensible(self) -> None:
        p = ConversionIntegrityPolicy()
        assert p.min_conversions_total == 1
        assert p.min_ratio_vs_baseline == 0.5
        assert p.require_all_baseline_goals_present is True

    def test_rejects_negative_min_total(self) -> None:
        with pytest.raises(ValidationError):
            ConversionIntegrityPolicy(min_conversions_total=-1)

    def test_rejects_ratio_above_one(self) -> None:
        with pytest.raises(ValidationError):
            ConversionIntegrityPolicy(min_ratio_vs_baseline=1.5)

    def test_rejects_negative_ratio(self) -> None:
        with pytest.raises(ValidationError):
            ConversionIntegrityPolicy(min_ratio_vs_baseline=-0.1)

    def test_rejects_extra_fields(self) -> None:
        with pytest.raises(ValidationError):
            ConversionIntegrityPolicy.model_validate({"unknown": 1})

    def test_policy_is_immutable(self) -> None:
        p = _ci_policy()
        with pytest.raises(ValidationError):
            p.min_conversions_total = 100  # type: ignore[misc]


class TestLoadConversionIntegrityPolicy:
    def test_loads_from_yaml(self, tmp_path: Path) -> None:
        path = tmp_path / "agent_policy.yml"
        path.write_text(
            """
min_conversions_total: 10
min_ratio_vs_baseline: 0.7
require_all_baseline_goals_present: false
""",
            encoding="utf-8",
        )

        p = load_conversion_integrity_policy(path)
        assert p.min_conversions_total == 10
        assert p.min_ratio_vs_baseline == 0.7
        assert p.require_all_baseline_goals_present is False

    def test_tolerates_unknown_keys(self, tmp_path: Path) -> None:
        path = tmp_path / "agent_policy.yml"
        path.write_text(
            """
account_daily_budget_cap_rub: 5000
min_conversions_total: 5
""",
            encoding="utf-8",
        )
        p = load_conversion_integrity_policy(path)
        assert p.min_conversions_total == 5


class TestConversionsSnapshot:
    def test_total_conversions_sums_goals(self) -> None:
        s = _snap(_goals((1, 10), (2, 5)))
        assert s.total_conversions() == 15

    def test_goal_ids_returns_set(self) -> None:
        s = _snap(_goals((1, 10), (2, 5)))
        assert s.goal_ids() == {1, 2}

    def test_find_returns_goal(self) -> None:
        s = _snap(_goals((1, 10), (2, 5)))
        assert s.find(2) is not None
        assert s.find(2).conversions == 5  # type: ignore[union-attr]
        assert s.find(999) is None

    def test_empty_snapshot_totals_zero(self) -> None:
        s = _snap([])
        assert s.total_conversions() == 0
        assert s.goal_ids() == set()


class TestConversionIntegrityHappyPath:
    def test_ok_when_current_matches_baseline(self) -> None:
        check = ConversionIntegrityCheck(_ci_policy())
        baseline = _snap(_goals((1, 100), (2, 50)))
        current = _snap(_goals((1, 100), (2, 50)))

        result = check.check(baseline, current)

        assert result.status == "ok"

    def test_ok_when_current_is_higher_than_baseline(self) -> None:
        check = ConversionIntegrityCheck(_ci_policy())
        baseline = _snap(_goals((1, 100)))
        current = _snap(_goals((1, 300)))

        result = check.check(baseline, current)

        assert result.status == "ok"

    def test_ok_when_new_goal_appears_in_current(self) -> None:
        # Additive change — a new goal doesn't break tracking.
        check = ConversionIntegrityCheck(_ci_policy())
        baseline = _snap(_goals((1, 100)))
        current = _snap(_goals((1, 100), (99, 5)))

        result = check.check(baseline, current)

        assert result.status == "ok"

    def test_ok_when_ratio_exactly_at_threshold(self) -> None:
        # Strict `<` blocks; equality is ok.
        check = ConversionIntegrityCheck(_ci_policy(min_ratio=0.5))
        baseline = _snap(_goals((1, 100)))
        current = _snap(_goals((1, 50)))  # ratio 0.5 exactly

        result = check.check(baseline, current)

        assert result.status == "ok"


class TestConversionIntegrityBlocks:
    def test_blocked_when_total_below_absolute_minimum(self) -> None:
        # No conversions at all for a whole window — tracking almost
        # certainly broken, block writes.
        check = ConversionIntegrityCheck(_ci_policy(min_total=1))
        baseline = _snap(_goals((1, 100)))
        current = _snap([])  # zero conversions

        result = check.check(baseline, current)

        assert result.status == "blocked"
        assert (
            "minimum" in (result.reason or "").lower() or "total" in (result.reason or "").lower()
        )
        assert result.details.get("current_total") == 0
        assert result.details.get("min_total") == 1

    def test_blocked_when_ratio_below_threshold(self) -> None:
        check = ConversionIntegrityCheck(_ci_policy(min_ratio=0.5))
        baseline = _snap(_goals((1, 100)))
        current = _snap(_goals((1, 40)))  # ratio 0.4 < 0.5

        result = check.check(baseline, current)

        assert result.status == "blocked"
        assert result.details.get("current_total") == 40
        assert result.details.get("baseline_total") == 100
        assert result.details.get("ratio") == 0.4
        assert result.details.get("min_ratio") == 0.5

    def test_blocked_when_baseline_goal_is_missing_in_current(self) -> None:
        # Goal 2 disappeared — could mean the goal was deleted or
        # tracking for it broke. Block until a human checks.
        check = ConversionIntegrityCheck(_ci_policy())
        baseline = _snap(_goals((1, 100), (2, 50)))
        current = _snap(_goals((1, 100)))  # goal 2 missing

        result = check.check(baseline, current)

        assert result.status == "blocked"
        missing = result.details.get("missing_goal_ids", [])
        assert 2 in missing

    def test_missing_goal_check_can_be_disabled(self) -> None:
        # If an operator intentionally removed a goal, they can turn
        # off the presence check via policy.
        check = ConversionIntegrityCheck(_ci_policy(require_goals=False))
        baseline = _snap(_goals((1, 100), (2, 50)))
        current = _snap(_goals((1, 100)))

        result = check.check(baseline, current)

        assert result.status == "ok"

    def test_blocks_first_failure(self) -> None:
        # Multiple failures in one snapshot — return the first.
        # Ordering: total-floor → ratio → missing-goals. Pinning so a
        # refactor that reorders is visible.
        check = ConversionIntegrityCheck(_ci_policy(min_total=10, min_ratio=0.8))
        baseline = _snap(_goals((1, 100), (2, 50)))
        current = _snap([])  # 0 total → trips min_total first

        result = check.check(baseline, current)

        assert result.status == "blocked"
        assert result.details.get("current_total") == 0


class TestConversionIntegrityEdgeCases:
    def test_warns_when_baseline_is_empty(self) -> None:
        # First-ever run with no historical data. Same pattern as
        # KS#5: emit warn so M2.3 audit surfaces it and M2.2
        # pipeline can refuse autonomous operation until the
        # baseline is filled.
        check = ConversionIntegrityCheck(_ci_policy())
        baseline = _snap([])
        current = _snap(_goals((1, 10)))

        result = check.check(baseline, current)

        assert result.status == "warn"
        assert "baseline" in (result.reason or "").lower()

    def test_warns_when_baseline_total_is_zero(self) -> None:
        # Goals exist in baseline but carried zero conversions —
        # either a low-traffic period or tracking was already broken.
        # Not blocked; a warn is the honest stance.
        check = ConversionIntegrityCheck(_ci_policy())
        baseline = _snap(_goals((1, 0), (2, 0)))
        current = _snap(_goals((1, 10)))

        result = check.check(baseline, current)

        assert result.status == "warn"

    def test_min_total_zero_disables_absolute_floor(self) -> None:
        # Zero floor means "don't check"; a completely empty current
        # is then allowed as long as ratio and goals survive.
        check = ConversionIntegrityCheck(_ci_policy(min_total=0, require_goals=False))
        baseline = _snap([])
        current = _snap([])

        result = check.check(baseline, current)

        # baseline empty → warn by the empty-baseline branch, not
        # blocked by the absolute floor.
        assert result.status == "warn"


class TestConversionIntegrityAuditorFindings:
    """Security-auditor findings on KS#6: runtime validation gap on
    GoalConversions, counter_id mismatch silent-pass, and an
    untested all-disabled-policy state.
    """

    def test_goal_conversions_rejects_negative_count(self) -> None:
        # MEDIUM. A negative count inflates total_conversions via
        # integer arithmetic and slips past the ratio check.
        with pytest.raises(ValueError, match="non-negative"):
            GoalConversions(goal_id=1, goal_name="g", conversions=-1)

    def test_goal_conversions_rejects_bool_count(self) -> None:
        # bool is int's subclass; reject explicitly so True/False
        # don't become 1/0.
        with pytest.raises(TypeError, match="conversions"):
            GoalConversions(goal_id=1, goal_name="g", conversions=True)  # type: ignore[arg-type]

    def test_goal_conversions_rejects_non_int_count(self) -> None:
        with pytest.raises(TypeError, match="conversions"):
            GoalConversions(goal_id=1, goal_name="g", conversions="10")  # type: ignore[arg-type]

    def test_goal_conversions_accepts_zero(self) -> None:
        g = GoalConversions(goal_id=1, goal_name="g", conversions=0)
        assert g.conversions == 0

    def test_blocks_on_counter_id_mismatch(self) -> None:
        # LOW. A pipeline bug (or deliberate poisoning) that pairs a
        # baseline from counter A with a current from counter B must
        # be caught — the ratio math would otherwise be nonsense.
        check = ConversionIntegrityCheck(_ci_policy())
        baseline = ConversionsSnapshot(counter_id=1, goals=_goals((1, 100)))
        current = ConversionsSnapshot(counter_id=2, goals=_goals((1, 100)))

        result = check.check(baseline, current)

        assert result.status == "blocked"
        assert "counter_id" in (result.reason or "").lower()
        assert result.details.get("baseline_counter_id") == 1
        assert result.details.get("current_counter_id") == 2

    def test_all_disabled_policy_passes_non_empty_baseline(self) -> None:
        # Test-coverage debt flagged by auditor: the code path where
        # every knob is off and baseline is non-empty was not pinned.
        # Today: falls through all three rules → ok.
        check = ConversionIntegrityCheck(
            _ci_policy(min_total=0, min_ratio=0.0, require_goals=False)
        )
        baseline = _snap(_goals((1, 100)))
        current = _snap([])

        result = check.check(baseline, current)

        assert result.status == "ok"


# ==========================================================================
# Kill-switch #7 — Query drift detector.
# ==========================================================================


def _queries(*qs: str, counter_id: int = 1) -> SearchQueriesSnapshot:
    return SearchQueriesSnapshot(counter_id=counter_id, queries=list(qs))


def _qd_policy(max_share: float = 0.4) -> QueryDriftPolicy:
    return QueryDriftPolicy(max_new_query_share=max_share)


class TestQueryDriftPolicyValidation:
    def test_default_share_is_forty_percent(self) -> None:
        p = QueryDriftPolicy()
        assert p.max_new_query_share == 0.4

    def test_accepts_boundary_values(self) -> None:
        for v in (0.0, 0.5, 1.0):
            assert _qd_policy(v).max_new_query_share == v

    def test_rejects_negative(self) -> None:
        with pytest.raises(ValidationError):
            _qd_policy(-0.1)

    def test_rejects_above_one(self) -> None:
        with pytest.raises(ValidationError):
            _qd_policy(1.5)

    def test_rejects_extra_fields(self) -> None:
        with pytest.raises(ValidationError):
            QueryDriftPolicy.model_validate({"unknown": 1})

    def test_policy_is_immutable(self) -> None:
        p = _qd_policy()
        with pytest.raises(ValidationError):
            p.max_new_query_share = 0.5  # type: ignore[misc]


class TestLoadQueryDriftPolicy:
    def test_loads_from_yaml(self, tmp_path: Path) -> None:
        path = tmp_path / "agent_policy.yml"
        path.write_text("max_new_query_share: 0.25\n", encoding="utf-8")
        p = load_query_drift_policy(path)
        assert p.max_new_query_share == 0.25

    def test_default_when_key_missing(self, tmp_path: Path) -> None:
        path = tmp_path / "agent_policy.yml"
        path.write_text("account_daily_budget_cap_rub: 5000\n", encoding="utf-8")
        p = load_query_drift_policy(path)
        assert p.max_new_query_share == 0.4


class TestSearchQueriesSnapshotNormalisation:
    def test_normalises_case_whitespace_and_dedupes(self) -> None:
        s = _queries("Купить обувь", "купить  обувь", "  КУПИТЬ обувь  ")
        assert s.normalised() == frozenset({"купить обувь"})

    def test_drops_empty_and_whitespace_only(self) -> None:
        s = _queries("", "   ", "купить обувь")
        assert s.normalised() == frozenset({"купить обувь"})

    def test_empty_snapshot_returns_empty_frozenset(self) -> None:
        s = _queries()
        assert s.normalised() == frozenset()


class TestQueryDriftHappyPath:
    def test_ok_when_current_is_subset_of_baseline(self) -> None:
        check = QueryDriftCheck(_qd_policy(0.4))
        baseline = _queries("a", "b", "c", "d", "e")
        current = _queries("a", "b")  # zero new

        result = check.check(baseline, current)

        assert result.status == "ok"

    def test_ok_when_new_share_below_threshold(self) -> None:
        # Threshold 0.4. Current: a,b,c,d (4 queries), baseline has
        # a,b,c (3 present), new = {d} → 1/4 = 0.25 < 0.4.
        check = QueryDriftCheck(_qd_policy(0.4))
        baseline = _queries("a", "b", "c")
        current = _queries("a", "b", "c", "d")

        result = check.check(baseline, current)

        assert result.status == "ok"

    def test_ok_when_new_share_exactly_at_threshold(self) -> None:
        # Strict `>` blocks; equality is ok. 2 new out of 5 = 0.4.
        check = QueryDriftCheck(_qd_policy(0.4))
        baseline = _queries("a", "b", "c")
        current = _queries("a", "b", "c", "d", "e")  # 2 new / 5 current

        result = check.check(baseline, current)

        assert result.status == "ok"

    def test_case_and_whitespace_variants_match(self) -> None:
        # "Купить обувь" in baseline and "  КУПИТЬ обувь  " in current
        # must be treated as the same query.
        check = QueryDriftCheck(_qd_policy(0.4))
        baseline = _queries("Купить обувь")
        current = _queries("  купить  обувь  ")  # same after normalisation

        result = check.check(baseline, current)

        assert result.status == "ok"


class TestQueryDriftBlocks:
    def test_blocked_when_new_share_above_threshold(self) -> None:
        # 3 new / 5 current = 0.6 > 0.4.
        check = QueryDriftCheck(_qd_policy(0.4))
        baseline = _queries("a", "b", "c")
        current = _queries("a", "b", "new1", "new2", "new3")

        result = check.check(baseline, current)

        assert result.status == "blocked"
        assert result.details.get("new_share") == pytest.approx(0.6)
        assert result.details.get("threshold") == 0.4
        # sample of offending queries surfaces for human review.
        sample = result.details.get("new_queries_sample", [])
        assert set(sample) <= {"new1", "new2", "new3"}
        assert result.details.get("current_size") == 5
        assert result.details.get("new_count") == 3

    def test_blocked_when_all_queries_are_new(self) -> None:
        check = QueryDriftCheck(_qd_policy(0.4))
        baseline = _queries("a", "b")
        current = _queries("x", "y", "z")  # 100% new

        result = check.check(baseline, current)

        assert result.status == "blocked"
        assert result.details.get("new_share") == pytest.approx(1.0)

    def test_blocks_on_counter_id_mismatch(self) -> None:
        # Same safeguard as KS#6: the two snapshots must describe the
        # same Metrika counter.
        check = QueryDriftCheck(_qd_policy(0.4))
        baseline = SearchQueriesSnapshot(counter_id=1, queries=["a"])
        current = SearchQueriesSnapshot(counter_id=2, queries=["a"])

        result = check.check(baseline, current)

        assert result.status == "blocked"
        assert "counter_id" in (result.reason or "").lower()


class TestQueryDriftEdgeCases:
    def test_warns_when_baseline_is_empty(self) -> None:
        # First-run / missing-backfill. Same pattern as KS#5/KS#6.
        check = QueryDriftCheck(_qd_policy(0.4))
        baseline = _queries()
        current = _queries("a", "b", "c")

        result = check.check(baseline, current)

        assert result.status == "warn"
        assert "baseline" in (result.reason or "").lower()

    def test_warns_when_current_is_empty(self) -> None:
        # No search queries observed — likely an ops issue, not
        # necessarily drift. Warn instead of block so the pipeline
        # surfaces it without aborting everything.
        check = QueryDriftCheck(_qd_policy(0.4))
        baseline = _queries("a", "b")
        current = _queries()

        result = check.check(baseline, current)

        assert result.status == "warn"

    def test_warns_when_both_are_empty(self) -> None:
        check = QueryDriftCheck(_qd_policy(0.4))
        baseline = _queries()
        current = _queries()

        result = check.check(baseline, current)

        assert result.status == "warn"

    def test_baseline_whitespace_and_case_do_not_inflate_drift(self) -> None:
        # Corner case: baseline has "ОБУВЬ" and "обувь " as two raw
        # entries; they must collapse to one before comparison.
        check = QueryDriftCheck(_qd_policy(0.4))
        baseline = _queries("ОБУВЬ", "обувь ")
        current = _queries("обувь", "сапоги")  # 50% new — above threshold

        # But the baseline's two raw entries collapse to {"обувь"}, so
        # this is 50% new regardless. Block.
        result = check.check(baseline, current)

        assert result.status == "blocked"
        assert result.details.get("current_size") == 2
        assert result.details.get("new_count") == 1

    def test_max_share_zero_blocks_any_new_query(self) -> None:
        # Operator wants zero drift tolerance.
        check = QueryDriftCheck(_qd_policy(0.0))
        baseline = _queries("a", "b")
        current = _queries("a", "b", "c")

        result = check.check(baseline, current)

        assert result.status == "blocked"

    def test_max_share_one_permits_all_new(self) -> None:
        check = QueryDriftCheck(_qd_policy(1.0))
        baseline = _queries("a")
        current = _queries("x", "y", "z")  # 100% new

        result = check.check(baseline, current)

        # 1.0 means "anything up to 100% is ok" — strict `>` means
        # even 100% passes (1.0 not > 1.0).
        assert result.status == "ok"

    def test_empty_current_warns_even_with_zero_threshold(self) -> None:
        # Auditor-flagged ordering guarantee: the empty-current
        # warn branch runs before the ratio comparison, so a
        # threshold=0.0 policy (which would normally block any new
        # query) still emits warn when current has no queries
        # rather than escalating to block.
        check = QueryDriftCheck(_qd_policy(0.0))
        baseline = _queries("a", "b")
        current = _queries()

        result = check.check(baseline, current)

        assert result.status == "warn"


# ==========================================================================
# M2.1 — Unified Policy schema.
# ==========================================================================


class TestPolicyConstruction:
    def test_minimal_policy_requires_only_budget_cap(self) -> None:
        # account_daily_budget_cap_rub is the one field that must be
        # set explicitly — everything else has a defensible default.
        p = Policy(budget_cap=BudgetCapPolicy(account_daily_budget_cap_rub=10_000))
        assert p.budget_cap.account_daily_budget_cap_rub == 10_000
        # Sub-slices default to empty / permissive.
        assert p.max_cpc.campaign_max_cpc_rub == {}
        assert p.negative_keyword_floor.required_negative_keywords == []
        assert p.quality_score_guard.min_quality_score_for_bid_increase == 5
        assert p.budget_balance_drift.max_shift_pct_per_day == 0.3
        assert p.conversion_integrity.min_conversions_total == 1
        assert p.query_drift.max_new_query_share == 0.4

    def test_default_approval_tiers_match_spec(self) -> None:
        p = Policy(budget_cap=BudgetCapPolicy(account_daily_budget_cap_rub=10_000))
        # §M2.1: auto-approve read-only + pause + negative keywords;
        # NOT resume.
        assert p.auto_approve_readonly is True
        assert p.auto_approve_pause is True
        assert p.auto_approve_resume is False
        assert p.auto_approve_negative_keywords is True

    def test_default_thresholds_match_spec(self) -> None:
        p = Policy(budget_cap=BudgetCapPolicy(account_daily_budget_cap_rub=10_000))
        assert p.max_daily_budget_change_pct == 0.2
        assert p.max_bid_increase_pct == 0.5
        assert p.max_bid_change_per_day_pct == 0.25
        assert p.max_bulk_size == 50

    def test_default_max_snapshot_age_seconds_is_300(self) -> None:
        """Five minutes — short enough that operator-driven apply-plan
        still works in normal workflow (read plans, type a command,
        apply), but tight enough that hours-old plans get rejected
        and re-issued against fresh data. Pinned so a future change
        surfaces as red rather than silently extending the
        TOCTOU window."""
        p = Policy(budget_cap=BudgetCapPolicy(account_daily_budget_cap_rub=10_000))
        assert p.max_snapshot_age_seconds == 300

    def test_rejects_zero_max_snapshot_age_seconds(self) -> None:
        """A zero ceiling would fail every apply-plan immediately
        (every snapshot is at least a few microseconds old by the
        time the executor reads it). Reject as a likely typo."""
        with pytest.raises(ValidationError):
            Policy(
                budget_cap=BudgetCapPolicy(account_daily_budget_cap_rub=10_000),
                max_snapshot_age_seconds=0,
            )

    def test_rejects_negative_max_snapshot_age_seconds(self) -> None:
        with pytest.raises(ValidationError):
            Policy(
                budget_cap=BudgetCapPolicy(account_daily_budget_cap_rub=10_000),
                max_snapshot_age_seconds=-1,
            )

    def test_default_forbidden_operations_match_spec(self) -> None:
        p = Policy(budget_cap=BudgetCapPolicy(account_daily_budget_cap_rub=10_000))
        assert set(p.forbidden_operations) == {
            "delete_campaigns",
            "delete_ads",
            "archive_campaigns_bulk",
        }

    def test_default_rollout_stage_is_shadow(self) -> None:
        # The most conservative stage — new deployments start here.
        p = Policy(budget_cap=BudgetCapPolicy(account_daily_budget_cap_rub=10_000))
        assert p.rollout_stage == "shadow"

    def test_policy_is_frozen(self) -> None:
        p = Policy(budget_cap=BudgetCapPolicy(account_daily_budget_cap_rub=10_000))
        with pytest.raises(ValidationError):
            p.rollout_stage = "autonomy_full"  # type: ignore[misc]

    def test_policy_rejects_extra_top_level_fields(self) -> None:
        # A typo in agent_policy.yml must be a loud error, not a
        # silent fallback to defaults.
        with pytest.raises(ValidationError):
            Policy.model_validate(
                {
                    "budget_cap": {"account_daily_budget_cap_rub": 10_000},
                    "totally_not_a_real_field": True,
                }
            )

    def test_rejects_invalid_rollout_stage(self) -> None:
        with pytest.raises(ValidationError):
            Policy.model_validate(
                {
                    "budget_cap": {"account_daily_budget_cap_rub": 10_000},
                    "rollout_stage": "yolo",
                }
            )

    def test_accepts_all_rollout_stages(self) -> None:
        for stage in ("shadow", "assist", "autonomy_light", "autonomy_full"):
            p = Policy(
                budget_cap=BudgetCapPolicy(account_daily_budget_cap_rub=10_000),
                rollout_stage=stage,  # type: ignore[arg-type]
            )
            assert p.rollout_stage == stage

    def test_rejects_zero_max_bulk_size(self) -> None:
        with pytest.raises(ValidationError):
            Policy(
                budget_cap=BudgetCapPolicy(account_daily_budget_cap_rub=10_000),
                max_bulk_size=0,
            )


class TestLoadPolicyFromFlatYaml:
    def test_loads_minimal_yaml(self, tmp_path: Path) -> None:
        path = tmp_path / "agent_policy.yml"
        path.write_text("account_daily_budget_cap_rub: 5000\n", encoding="utf-8")

        policy = load_policy(path)

        assert policy.budget_cap.account_daily_budget_cap_rub == 5000

    def test_loads_fields_from_every_kill_switch(self, tmp_path: Path) -> None:
        path = tmp_path / "agent_policy.yml"
        path.write_text(
            """
account_daily_budget_cap_rub: 10000
campaign_group_caps_rub:
  brand: 3000
campaign_max_cpc_rub:
  100: 25.0
required_negative_keywords:
  - бесплатно
min_quality_score_for_bid_increase: 6
max_shift_pct_per_day: 0.25
min_conversions_total: 3
min_ratio_vs_baseline: 0.7
require_all_baseline_goals_present: false
max_new_query_share: 0.35
""",
            encoding="utf-8",
        )

        policy = load_policy(path)

        assert policy.budget_cap.account_daily_budget_cap_rub == 10_000
        assert policy.budget_cap.campaign_group_caps_rub == {"brand": 3000}
        assert policy.max_cpc.campaign_max_cpc_rub == {100: 25.0}
        assert policy.negative_keyword_floor.required_negative_keywords == ["бесплатно"]
        assert policy.quality_score_guard.min_quality_score_for_bid_increase == 6
        assert policy.budget_balance_drift.max_shift_pct_per_day == 0.25
        assert policy.conversion_integrity.min_conversions_total == 3
        assert policy.conversion_integrity.min_ratio_vs_baseline == 0.7
        assert policy.conversion_integrity.require_all_baseline_goals_present is False
        assert policy.query_drift.max_new_query_share == 0.35

    def test_loads_top_level_fields(self, tmp_path: Path) -> None:
        path = tmp_path / "agent_policy.yml"
        path.write_text(
            """
account_daily_budget_cap_rub: 10000
auto_approve_resume: true
max_daily_budget_change_pct: 0.3
max_bid_increase_pct: 0.4
max_bulk_size: 100
rollout_stage: assist
forbidden_operations:
  - delete_campaigns
""",
            encoding="utf-8",
        )

        policy = load_policy(path)

        assert policy.auto_approve_resume is True
        assert policy.max_daily_budget_change_pct == 0.3
        assert policy.max_bid_increase_pct == 0.4
        assert policy.max_bulk_size == 100
        assert policy.rollout_stage == "assist"
        assert policy.forbidden_operations == ["delete_campaigns"]

    def test_rejects_unknown_yaml_key(self, tmp_path: Path) -> None:
        # A typo must not silently default — we want a loud error
        # pointing at the offending key.
        path = tmp_path / "agent_policy.yml"
        path.write_text(
            """
account_daily_budget_cap_rub: 10000
accuont_daliy_budget_cap_rub: 5000
""",
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match="unknown keys"):
            load_policy(path)

    def test_rejects_missing_account_cap(self, tmp_path: Path) -> None:
        # budget_cap.account_daily_budget_cap_rub has no default —
        # the load must fail explicitly rather than construct a
        # permissive policy.
        path = tmp_path / "agent_policy.yml"
        path.write_text(
            """
auto_approve_resume: true
""",
            encoding="utf-8",
        )

        with pytest.raises(ValidationError):
            load_policy(path)

    def test_loads_the_shipped_example_yml(self) -> None:
        # Smoke test against the committed example. A regression here
        # means we shipped a broken sample to operators.
        example_path = Path(__file__).parent.parent.parent.parent / "agent_policy.example.yml"
        # Only run if the file exists; otherwise skip silently.
        if not example_path.exists():
            pytest.skip(f"example policy not present at {example_path}")

        policy = load_policy(example_path)
        assert policy.budget_cap.account_daily_budget_cap_rub > 0


class TestPolicyAuditorFindings:
    """Security-auditor findings on the M2.1 unified Policy."""

    def test_rejects_empty_forbidden_operations_entry(self) -> None:
        # MEDIUM: a blank "" silently replaces defaults without
        # providing a real block — typo or whitespace-only entries
        # must raise.
        with pytest.raises(ValidationError, match="forbidden_operations"):
            Policy(
                budget_cap=BudgetCapPolicy(account_daily_budget_cap_rub=10_000),
                forbidden_operations=[""],
            )

    def test_rejects_whitespace_only_forbidden_operations_entry(self) -> None:
        with pytest.raises(ValidationError, match="forbidden_operations"):
            Policy(
                budget_cap=BudgetCapPolicy(account_daily_budget_cap_rub=10_000),
                forbidden_operations=["   "],
            )

    def test_forbidden_operations_are_normalised(self) -> None:
        # Case drift and surrounding whitespace collapse so the M2.2
        # pipeline's comparator can do a case-insensitive lookup
        # without each call re-lowercasing.
        policy = Policy(
            budget_cap=BudgetCapPolicy(account_daily_budget_cap_rub=10_000),
            forbidden_operations=["Delete_Campaigns", "  archive_campaigns_bulk  "],
        )
        assert policy.forbidden_operations == [
            "delete_campaigns",
            "archive_campaigns_bulk",
        ]

    def test_rejects_oversize_policy_file(self, tmp_path: Path) -> None:
        # LOW: yaml.safe_load prevents arbitrary code execution but
        # not unbounded memory expansion. Pin the 64 KiB guard.
        path = tmp_path / "agent_policy.yml"
        # 100 KB file — valid YAML, just huge.
        filler = " " * (100 * 1024)
        path.write_text(
            f"account_daily_budget_cap_rub: 10000\n# padding:{filler}\n",
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match="safety cap"):
            load_policy(path)

    def test_key_maps_stay_in_sync_with_slice_model_fields(self) -> None:
        # Maintenance trap: adding a field to a slice-policy without
        # also adding the key to _*_KEYS would make load_policy
        # silently reject the valid YAML key as "unknown". Pin the
        # invariant.
        from yadirect_agent.agent import safety

        assert set(BudgetCapPolicy.model_fields) == safety._BUDGET_CAP_KEYS
        assert set(MaxCpcPolicy.model_fields) == safety._MAX_CPC_KEYS
        assert set(NegativeKeywordFloorPolicy.model_fields) == safety._NK_FLOOR_KEYS
        assert set(QualityScoreGuardPolicy.model_fields) == safety._QS_GUARD_KEYS
        assert set(BudgetBalanceDriftPolicy.model_fields) == safety._BALANCE_DRIFT_KEYS
        assert set(ConversionIntegrityPolicy.model_fields) == safety._CONVERSION_KEYS
        assert set(QueryDriftPolicy.model_fields) == safety._QUERY_DRIFT_KEYS


class TestLoadPolicyBackwardsCompat:
    """The individual load_*_policy helpers MUST keep working. Existing
    kill-switch tests rely on them, and field-level callers shouldn't
    be forced to load the unified schema if they only want one slice.
    """

    def test_load_budget_cap_policy_still_works(self, tmp_path: Path) -> None:
        path = tmp_path / "p.yml"
        path.write_text("account_daily_budget_cap_rub: 1500\n", encoding="utf-8")
        p = load_budget_cap_policy(path)
        assert p.account_daily_budget_cap_rub == 1500

    def test_load_query_drift_policy_still_works(self, tmp_path: Path) -> None:
        path = tmp_path / "p.yml"
        path.write_text("max_new_query_share: 0.2\n", encoding="utf-8")
        p = load_query_drift_policy(path)
        assert p.max_new_query_share == 0.2


# --------------------------------------------------------------------------
# M20 slice 4 — every kill-switch emits ``policy_slack`` (distance to
# threshold) into ``CheckResult.details`` so the @requires_plan decorator
# can auto-populate ``Rationale.policy_slack``. The decorator-side merge
# is tested in tests/unit/agent/test_executor_rationale.py; this section
# pins the per-check emission contract.
#
# Sign convention pinned across all checks: positive = headroom (we are
# below / inside the threshold by that amount); negative = we are over
# the threshold by that magnitude. Operators reading shadow-week
# rationale see "max_cpc: 12.5 RUB headroom" (ok) vs "max_cpc: -2.5 RUB
# over cap" (blocked) without consulting separate ok/blocked context.
# --------------------------------------------------------------------------


class TestKs1BudgetCapEmitsPolicySlack:
    """KS#1: ``slack = account_cap - projected_total`` (RUB)."""

    def test_ok_path_emits_positive_slack_equal_to_headroom(self) -> None:
        check = BudgetCapCheck(_policy(account_cap=10_000))
        snapshot = AccountBudgetSnapshot(campaigns=[_campaign(1, 5_000)])

        result = check.check(snapshot, [BudgetChange(campaign_id=1, new_daily_budget_rub=8_500)])

        assert result.status == "ok"
        # 10_000 cap - 8_500 projected = 1_500 RUB headroom.
        assert result.details["policy_slack"] == pytest.approx(1_500.0)

    def test_blocked_path_emits_negative_slack_equal_to_overshoot(self) -> None:
        # The whole point of slack: same number on both sides of the
        # threshold, sign tells the story. A regression that emits 0
        # or omitted on the blocked path would silently destroy the
        # "how close were we" signal that shadow-week calibration
        # depends on.
        check = BudgetCapCheck(_policy(account_cap=10_000))
        snapshot = AccountBudgetSnapshot(campaigns=[_campaign(1, 5_000)])

        result = check.check(snapshot, [BudgetChange(campaign_id=1, new_daily_budget_rub=12_000)])

        assert result.status == "blocked"
        # 10_000 - 12_000 = -2_000 (we'd exceed by 2_000 RUB).
        assert result.details["policy_slack"] == pytest.approx(-2_000.0)


class TestKs2MaxCpcEmitsPolicySlack:
    """KS#2: ``slack = min(cap_per_kw - max(new_bids))`` across constrained
    keywords (RUB). When no keyword in the batch has a configured cap,
    slack is undefined and the key is absent."""

    def test_ok_path_emits_positive_slack_for_constrained_keyword(self) -> None:
        check = MaxCpcCheck(_cpc_policy({100: 50.0}))
        snapshot = AccountBidSnapshot(keywords=[_keyword(1, campaign_id=100, search=10.0)])

        result = check.check(
            snapshot,
            [ProposedBidChange(keyword_id=1, new_search_bid_rub=30.0)],
        )

        assert result.status == "ok"
        # cap 50 - bid 30 = 20 RUB headroom on the only constrained kw.
        assert result.details["policy_slack"] == pytest.approx(20.0)

    def test_blocked_path_emits_negative_slack_for_overshooting_keyword(self) -> None:
        check = MaxCpcCheck(_cpc_policy({100: 50.0}))
        snapshot = AccountBidSnapshot(keywords=[_keyword(1, campaign_id=100, search=10.0)])

        result = check.check(
            snapshot,
            [ProposedBidChange(keyword_id=1, new_search_bid_rub=60.0)],
        )

        assert result.status == "blocked"
        # 50 - 60 = -10 (over cap by 10 RUB).
        assert result.details["policy_slack"] == pytest.approx(-10.0)

    def test_no_constrained_keyword_omits_slack(self) -> None:
        # A campaign with no entry in campaign_max_cpc_rub is
        # unconstrained by KS#2; slack is undefined. Decorator must
        # gracefully skip the missing key (tested decorator-side).
        check = MaxCpcCheck(_cpc_policy({}))
        snapshot = AccountBidSnapshot(keywords=[_keyword(1, campaign_id=100, search=10.0)])

        result = check.check(
            snapshot,
            [ProposedBidChange(keyword_id=1, new_search_bid_rub=30.0)],
        )

        assert result.status == "ok"
        assert "policy_slack" not in result.details


class TestKs3NegativeKeywordFloorEmitsPolicySlack:
    """KS#3: ``slack = -len(missing)`` (count). Zero when the campaign has
    every required negative; negative when phrases are missing. Same
    sign convention as the others: zero / positive = safe."""

    def test_ok_path_emits_zero_slack_when_all_required_present(self) -> None:
        check = NegativeKeywordFloorCheck(_nk_policy(["foo", "bar"]))
        snapshot = AccountBudgetSnapshot(
            campaigns=[_campaign_with_kw(1, negatives=["foo", "bar", "baz"])],
        )

        result = check.check(
            snapshot,
            [BudgetChange(campaign_id=1, new_state="ON")],
        )

        assert result.status == "ok"
        # Zero missing → zero slack. The campaign has exactly the
        # required floor (plus extras); operator sees "no headroom
        # but no violation" — accurate.
        assert result.details["policy_slack"] == pytest.approx(0.0)

    def test_blocked_path_emits_negative_slack_equal_to_minus_missing_count(self) -> None:
        check = NegativeKeywordFloorCheck(_nk_policy(["foo", "bar", "baz"]))
        snapshot = AccountBudgetSnapshot(
            campaigns=[_campaign_with_kw(1, negatives=["foo"])],
        )

        result = check.check(
            snapshot,
            [BudgetChange(campaign_id=1, new_state="ON")],
        )

        assert result.status == "blocked"
        # Two phrases missing → slack=-2.
        assert result.details["policy_slack"] == pytest.approx(-2.0)


class TestKs4QualityScoreGuardEmitsPolicySlack:
    """KS#4: ``slack = min(current_qs - threshold)`` across keywords with
    an explicit bid INCREASE. Decreases / no-op bids are unaffected by
    the QS guard, so they don't contribute to the slack window."""

    def test_ok_path_emits_positive_slack_for_increase_above_threshold(self) -> None:
        check = QualityScoreGuardCheck(_qs_policy(threshold=5))
        snapshot = AccountBidSnapshot(
            keywords=[_kw_with_qs(1, qs=8, search=5.0)],
        )

        result = check.check(
            snapshot,
            [ProposedBidChange(keyword_id=1, new_search_bid_rub=10.0)],  # increase
        )

        assert result.status == "ok"
        # qs 8 - threshold 5 = 3 QS points headroom.
        assert result.details["policy_slack"] == pytest.approx(3.0)

    def test_blocked_path_emits_negative_slack_for_low_qs_increase(self) -> None:
        check = QualityScoreGuardCheck(_qs_policy(threshold=5))
        snapshot = AccountBidSnapshot(
            keywords=[_kw_with_qs(1, qs=3, search=5.0)],
        )

        result = check.check(
            snapshot,
            [ProposedBidChange(keyword_id=1, new_search_bid_rub=10.0)],  # increase
        )

        assert result.status == "blocked"
        # qs 3 - threshold 5 = -2.
        assert result.details["policy_slack"] == pytest.approx(-2.0)


class TestKs5BudgetBalanceDriftEmitsPolicySlack:
    """KS#5: ``slack = max_shift_pct - actual_shift_pct``. Both sides
    in PCT (0..1)."""

    def test_ok_path_emits_positive_slack_for_small_drift(self) -> None:
        check = BudgetBalanceDriftCheck(_bbd_policy(max_shift=0.3))
        baseline = AccountBudgetSnapshot(campaigns=[_ab_campaign(1, 1_000)])
        snapshot = AccountBudgetSnapshot(campaigns=[_ab_campaign(1, 1_100)])

        # +10% drift, threshold 30% → 20% headroom.
        result = check.check(baseline, snapshot, [])

        assert result.status == "ok"
        assert result.details["policy_slack"] == pytest.approx(0.2)

    def test_blocked_path_emits_negative_slack_for_large_drift(self) -> None:
        check = BudgetBalanceDriftCheck(_bbd_policy(max_shift=0.3))
        baseline = AccountBudgetSnapshot(campaigns=[_ab_campaign(1, 1_000)])
        snapshot = AccountBudgetSnapshot(campaigns=[_ab_campaign(1, 1_500)])

        # +50% drift, threshold 30% → -20% (over by 20pp).
        result = check.check(baseline, snapshot, [])

        assert result.status == "blocked"
        assert result.details["policy_slack"] == pytest.approx(-0.2)


class TestKs6ConversionIntegrityEmitsPolicySlack:
    """KS#6: ``slack = ratio - min_ratio``. Emit on the ratio path only;
    counter-mismatch / missing-goals / empty-baseline paths skip slack
    (the failure mode is structural, not a numeric distance)."""

    def test_ok_path_emits_positive_slack_for_healthy_ratio(self) -> None:
        check = ConversionIntegrityCheck(_ci_policy(min_total=0, min_ratio=0.5))
        baseline = _snap(_goals((1, 100)))
        current = _snap(_goals((1, 80)))  # ratio 0.8

        result = check.check(baseline, current)

        assert result.status == "ok"
        # 0.8 - 0.5 = 0.3 ratio headroom.
        assert result.details["policy_slack"] == pytest.approx(0.3)

    def test_ratio_blocked_path_emits_negative_slack(self) -> None:
        check = ConversionIntegrityCheck(_ci_policy(min_total=0, min_ratio=0.5))
        baseline = _snap(_goals((1, 100)))
        current = _snap(_goals((1, 20)))  # ratio 0.2

        result = check.check(baseline, current)

        assert result.status == "blocked"
        # 0.2 - 0.5 = -0.3.
        assert result.details["policy_slack"] == pytest.approx(-0.3)


class TestKs7QueryDriftEmitsPolicySlack:
    """KS#7: ``slack = max_new_query_share - actual_new_share`` (both
    in 0..1 PCT)."""

    def test_ok_path_emits_positive_slack_for_low_drift(self) -> None:
        check = QueryDriftCheck(_qd_policy(max_share=0.4))
        baseline = _queries("a", "b", "c", "d", "e")
        # 1 of 5 queries new = 20% share, well under 40%.
        current = _queries("a", "b", "c", "d", "NEW")

        result = check.check(baseline, current)

        assert result.status == "ok"
        # 0.4 - 0.2 = 0.2 share headroom.
        assert result.details["policy_slack"] == pytest.approx(0.2)

    def test_blocked_path_emits_negative_slack_for_high_drift(self) -> None:
        check = QueryDriftCheck(_qd_policy(max_share=0.4))
        baseline = _queries("a", "b", "c", "d", "e")
        # 4 of 5 new = 80% share, above 40%.
        current = _queries("X", "Y", "Z", "W", "e")

        result = check.check(baseline, current)

        assert result.status == "blocked"
        # 0.4 - 0.8 = -0.4.
        assert result.details["policy_slack"] == pytest.approx(-0.4)
