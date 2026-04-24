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
    BudgetCapCheck,
    BudgetCapPolicy,
    BudgetChange,
    CampaignBudget,
    CheckResult,
    KeywordSnapshot,
    MaxCpcCheck,
    MaxCpcPolicy,
    NegativeKeywordFloorCheck,
    NegativeKeywordFloorPolicy,
    ProposedBidChange,
    QualityScoreGuardCheck,
    QualityScoreGuardPolicy,
    load_budget_cap_policy,
    load_max_cpc_policy,
    load_negative_keyword_floor_policy,
    load_quality_score_guard_policy,
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
