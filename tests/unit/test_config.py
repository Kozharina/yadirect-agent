"""Tests for ``Settings`` field validators.

Most config fields are trusted to pydantic's built-in validators, but
two fields have additional invariants that need explicit tests:

- ``account_target_cpa_rub``: ``gt=0`` does not reject IEEE-754
  ``inf`` and ``-inf`` (``inf > 0`` is True), and ``nan``
  comparison semantics are unstable. We validate finite-positive
  explicitly. (auditor M15.5.1 MEDIUM-2.)
"""

from __future__ import annotations

import math

import pytest
from pydantic import SecretStr, ValidationError

from yadirect_agent.config import Settings


def _settings_kwargs(**overrides: object) -> dict[str, object]:
    """Build the minimum kwargs for a valid Settings construction."""
    base: dict[str, object] = {
        "yandex_direct_token": SecretStr("test"),
        "yandex_metrika_token": SecretStr("test"),
        "anthropic_api_key": SecretStr("test"),
        "yandex_use_sandbox": True,
        "agent_max_daily_budget_rub": 5000,
    }
    base.update(overrides)
    return base


class TestAccountTargetCpaRub:
    def test_default_none_accepted(self) -> None:
        settings = Settings(**_settings_kwargs())  # type: ignore[arg-type]

        assert settings.account_target_cpa_rub is None

    def test_finite_positive_accepted(self) -> None:
        settings = Settings(**_settings_kwargs(account_target_cpa_rub=600.0))  # type: ignore[arg-type]

        assert settings.account_target_cpa_rub == pytest.approx(600.0)

    def test_zero_rejected(self) -> None:
        # gt=0 catches this — already covered by pydantic, but pinning
        # for completeness.
        with pytest.raises(ValidationError):
            Settings(**_settings_kwargs(account_target_cpa_rub=0.0))  # type: ignore[arg-type]

    def test_negative_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Settings(**_settings_kwargs(account_target_cpa_rub=-100.0))  # type: ignore[arg-type]

    def test_positive_infinity_rejected(self) -> None:
        # The auditor's exploit: ``inf > 0`` is True, so the gt=0
        # validator alone accepts it. Without this guard, the
        # HighCpaRule would silently become a no-op (every campaign
        # has cpa < inf) and the operator would never see CPA
        # findings. (M15.5.1 MEDIUM-2.)
        with pytest.raises(ValidationError):
            Settings(**_settings_kwargs(account_target_cpa_rub=math.inf))  # type: ignore[arg-type]

    def test_negative_infinity_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Settings(**_settings_kwargs(account_target_cpa_rub=-math.inf))  # type: ignore[arg-type]

    def test_nan_rejected(self) -> None:
        # NaN comparisons are False both ways, leading to weird rule
        # behaviour: ``cpa <= nan`` is False so the rule falls through
        # to its finding-emit block, which produces ``nan`` impact —
        # crashes ``json.dumps`` in --json mode.
        with pytest.raises(ValidationError):
            Settings(**_settings_kwargs(account_target_cpa_rub=math.nan))  # type: ignore[arg-type]


class TestUsdToRubRate:
    def test_default_is_100(self) -> None:
        settings = Settings(**_settings_kwargs())  # type: ignore[arg-type]

        assert settings.usd_to_rub_rate == pytest.approx(100.0)

    def test_zero_rejected(self) -> None:
        # gt=0; a zero rate would zero out every cost calculation.
        with pytest.raises(ValidationError):
            Settings(**_settings_kwargs(usd_to_rub_rate=0))  # type: ignore[arg-type]

    def test_inf_rejected(self) -> None:
        # auditor M15.5.1 MEDIUM-2 pattern.
        with pytest.raises(ValidationError):
            Settings(**_settings_kwargs(usd_to_rub_rate=math.inf))  # type: ignore[arg-type]


class TestAgentMonthlyLlmBudgetRub:
    def test_default_none(self) -> None:
        settings = Settings(**_settings_kwargs())  # type: ignore[arg-type]

        assert settings.agent_monthly_llm_budget_rub is None

    def test_finite_positive_accepted(self) -> None:
        settings = Settings(**_settings_kwargs(agent_monthly_llm_budget_rub=3000.0))  # type: ignore[arg-type]

        assert settings.agent_monthly_llm_budget_rub == pytest.approx(3000.0)

    def test_zero_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Settings(**_settings_kwargs(agent_monthly_llm_budget_rub=0))  # type: ignore[arg-type]

    def test_inf_rejected(self) -> None:
        # An inf budget would silently defeat any future enforcement
        # path (M21.2). Reject at construction.
        with pytest.raises(ValidationError):
            Settings(**_settings_kwargs(agent_monthly_llm_budget_rub=math.inf))  # type: ignore[arg-type]
