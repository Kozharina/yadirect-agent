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
    ProposedBidChange,
    load_budget_cap_policy,
    load_max_cpc_policy,
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
