"""Tests for ``generate_policy_proposal`` (M15.4 slice 3).

The proposal is a pure function over (BusinessProfile,
current_active_daily_total_rub) → ``{policy_yaml, summary}``.
Pin both the numeric formula and the YAML round-trip through
``load_policy``: a YAML the operator pastes into
``agent_policy.yml`` MUST parse cleanly into the same ``Policy``
the runtime would reject if mis-shaped.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from yadirect_agent.services.policy_proposal import generate_policy_proposal

from yadirect_agent.agent.safety import load_policy
from yadirect_agent.models.business_profile import BusinessProfile


def _profile(
    *,
    monthly_budget_rub: int = 60_000,
    target_cpa_rub: int | None = None,
) -> BusinessProfile:
    return BusinessProfile(
        niche="Online courses on woodworking",
        monthly_budget_rub=monthly_budget_rub,
        target_cpa_rub=target_cpa_rub,
    )


class TestProposalFormula:
    def test_cap_is_1_2x_current_when_current_above_monthly_avg(self) -> None:
        # Current daily sum 10_000 RUB → 1.2x = 12_000 (already
        # round). Monthly 60_000/30 = 2000 RUB/day fallback. The
        # spec formula wins (1.2 x current > monthly/30) → 12_000.
        result = generate_policy_proposal(
            profile=_profile(monthly_budget_rub=60_000),
            current_active_daily_total_rub=10_000.0,
        )

        assert result["summary"]["chosen_account_daily_budget_cap_rub"] == 12_000

    def test_cap_falls_back_to_monthly_avg_when_current_low(self) -> None:
        # Sandbox / fresh account where current=0 → spec formula
        # alone yields 0, leaving the agent unable to do anything.
        # Fallback to monthly/30: 90_000 / 30 = 3000 RUB/day.
        result = generate_policy_proposal(
            profile=_profile(monthly_budget_rub=90_000),
            current_active_daily_total_rub=0.0,
        )

        assert result["summary"]["chosen_account_daily_budget_cap_rub"] == 3_000

    def test_cap_takes_max_of_two_formulas(self) -> None:
        # 1.2 x 1000 = 1200; monthly 90_000/30 = 3000. The
        # operator-meaningful intent is "let the agent reach the
        # full monthly", so monthly/30 wins when it's larger.
        result = generate_policy_proposal(
            profile=_profile(monthly_budget_rub=90_000),
            current_active_daily_total_rub=1_000.0,
        )

        assert result["summary"]["chosen_account_daily_budget_cap_rub"] == 3_000

    def test_cap_rounds_up_to_nearest_100(self) -> None:
        # 1.2 x 9_983 = 11_979.6 → ceil_to_100 = 12_000. Operator
        # readability: a YAML with ``11_979`` is uglier and
        # hand-editing it nudges toward 12_000 anyway.
        result = generate_policy_proposal(
            profile=_profile(monthly_budget_rub=10_000),
            current_active_daily_total_rub=9_983.0,
        )

        assert result["summary"]["chosen_account_daily_budget_cap_rub"] == 12_000

    def test_cap_handles_minimum_profile(self) -> None:
        # Smallest legal profile: 1000 RUB/month, current=0.
        # Monthly/30 = 33.33 → ceil_to_100 = 100. Below Direct's
        # daily floor (300) but at least non-zero, so the agent
        # can be brought up; the operator sees the small number
        # in the summary and can override the YAML before
        # applying. We do NOT silently clamp to 300 — that would
        # hide the input/output mismatch.
        result = generate_policy_proposal(
            profile=_profile(monthly_budget_rub=1_000),
            current_active_daily_total_rub=0.0,
        )

        assert result["summary"]["chosen_account_daily_budget_cap_rub"] == 100


class TestProposalSummary:
    def test_summary_carries_inputs_and_chosen(self) -> None:
        # The LLM uses the summary to explain the number to the
        # operator without re-deriving it. Pin the keys so a
        # future refactor can't silently drop a field the LLM
        # was relying on.
        result = generate_policy_proposal(
            profile=_profile(monthly_budget_rub=60_000),
            current_active_daily_total_rub=4_000.0,
        )

        s = result["summary"]
        assert s["current_active_daily_total_rub"] == 4_000.0
        assert s["monthly_budget_rub"] == 60_000
        assert s["monthly_budget_avg_daily_rub"] == 2_000
        assert s["margin_factor"] == 1.2
        assert s["cap_from_current_rub"] == 4_800
        assert s["cap_from_monthly_avg_rub"] == 2_000
        assert s["chosen_account_daily_budget_cap_rub"] == 4_800
        assert "formula" in s


class TestProposalYaml:
    def test_yaml_parses_via_load_policy(self, tmp_path: Path) -> None:
        # End-to-end pin: the YAML the operator copy-pastes into
        # ``agent_policy.yml`` MUST round-trip through the live
        # loader — the same loader the runtime uses. A regression
        # that emits an unknown key, wrong type, or stale field
        # name would fail here loudly rather than at the operator's
        # desk.
        result = generate_policy_proposal(
            profile=_profile(monthly_budget_rub=60_000),
            current_active_daily_total_rub=10_000.0,
        )

        path = tmp_path / "agent_policy.yml"
        path.write_text(result["policy_yaml"], encoding="utf-8")
        policy = load_policy(path)

        assert policy.budget_cap.account_daily_budget_cap_rub == 12_000
        assert policy.rollout_stage == "shadow"

    def test_yaml_is_flat_format(self) -> None:
        # ``load_policy`` accepts the flat YAML format
        # (``account_daily_budget_cap_rub`` at top level, not
        # nested under ``budget_cap``). The proposal must use
        # the same format so operators can hand-edit without
        # learning two shapes.
        result = generate_policy_proposal(
            profile=_profile(monthly_budget_rub=60_000),
            current_active_daily_total_rub=10_000.0,
        )

        parsed = yaml.safe_load(result["policy_yaml"])
        assert "account_daily_budget_cap_rub" in parsed
        assert "budget_cap" not in parsed  # flat, not nested

    def test_yaml_starts_with_provenance_comment(self) -> None:
        # The operator opens ``agent_policy.yml`` weeks later
        # and asks "where did this number come from?". The
        # comment header carries the answer (inputs + tool name)
        # so they don't have to reconstruct context.
        result = generate_policy_proposal(
            profile=_profile(monthly_budget_rub=60_000),
            current_active_daily_total_rub=10_000.0,
        )

        first_line = result["policy_yaml"].splitlines()[0]
        assert first_line.startswith("#")
        assert "yadirect-agent" in result["policy_yaml"]

    def test_yaml_seeds_rollout_stage_shadow(self) -> None:
        # Defence-in-depth: even if the operator skips reading
        # the YAML and just applies it, ``rollout_stage: shadow``
        # means the agent is read-only by default. Mutations
        # require an explicit promote.
        result = generate_policy_proposal(
            profile=_profile(),
            current_active_daily_total_rub=5_000.0,
        )

        parsed = yaml.safe_load(result["policy_yaml"])
        assert parsed["rollout_stage"] == "shadow"


class TestProposalEdgeCases:
    def test_negative_current_total_rejected(self) -> None:
        # An accidentally-passed negative would silently shift
        # the cap formula — better to fail loudly at the helper
        # boundary.
        with pytest.raises(ValueError, match="current_active_daily_total_rub"):
            generate_policy_proposal(
                profile=_profile(),
                current_active_daily_total_rub=-1.0,
            )
