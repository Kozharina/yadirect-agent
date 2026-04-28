"""Central configuration. Loaded once from env, passed explicitly everywhere.

Design choices:
- pydantic-settings for typed config with validation at startup (fail fast).
- No global singleton — we pass Settings into clients/services via DI so
  tests can swap it out cleanly.
- SecretStr for tokens so they never accidentally end up in logs.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Yandex ---
    yandex_direct_token: SecretStr = Field(default=SecretStr(""))
    yandex_metrika_token: SecretStr = Field(default=SecretStr(""))
    yandex_client_login: str | None = None
    yandex_use_sandbox: bool = True

    # M6 (Metrika reading) — counter ID is required for any read against
    # the user's analytics data. We keep it None by default so the agent
    # boots even if Metrika integration is not configured (read-only paths
    # like list_campaigns still work). Services that need it raise a
    # clear ConfigError when it's missing rather than crashing on the
    # first HTTP call. Single counter only — multi-counter is M14
    # (agency mode) territory.
    yandex_metrika_counter_id: int | None = Field(default=None, ge=1)

    # M15.5.1 health check — account-wide target CPA in RUB. Used by
    # the high-CPA rule to flag campaigns spending above the operator's
    # acceptable cost-per-acquisition. Optional; rules that need it
    # silently skip when None — better than firing on every campaign.
    # A future M11 milestone will add per-campaign targets that override
    # this account-wide value.
    account_target_cpa_rub: float | None = Field(default=None, gt=0)

    # --- M21 cost tracking ---

    # USD → RUB conversion rate. Default 100 is a reasonable round
    # number for read-back-friendly cost estimates; operators with a
    # specific accounting rate (e.g. CBR fixing) override via env or
    # .env. Per-record snapshots in CostRecord preserve the exact
    # rate used at write time, so changing this here only affects
    # future records, not historical cost.
    usd_to_rub_rate: float = Field(default=100.0, gt=0)

    # Optional monthly LLM-spend budget in RUB. None ⇒ no enforcement
    # (observability only via ``yadirect-agent cost status``). Hard
    # auto-degrade to ``--no-llm`` mode is M21.2 follow-up — needs M18
    # for the alert path before we can enforce silently.
    agent_monthly_llm_budget_rub: float | None = Field(default=None, gt=0)

    # --- Anthropic ---
    anthropic_api_key: SecretStr = Field(default=SecretStr(""))
    anthropic_model: str = "claude-opus-4-7"

    # --- Agent ---
    agent_policy_path: Path = Path("./agent_policy.yml")
    # M2.4 env-backstop: ``ge=1`` rather than ``ge=0`` rejects both the
    # negative-typo trap (``-1`` would silently propagate through
    # ``min(yaml, env)`` into a negative Policy cap that KS#1 cannot
    # interpret cleanly) and the zero "freeze the agent" anti-pattern
    # (the right way to disable the agent is ``rollout_stage="shadow"``
    # in the policy YAML, not a misleading budget=0 that produces a
    # generic "cap exceeded" rejection on every mutation). Auditor
    # PR M2.4 MEDIUM-1.
    agent_max_daily_budget_rub: int = Field(default=10_000, ge=1)

    # --- Observability ---
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["json", "console"] = "json"
    audit_log_path: Path = Path("./logs/audit.jsonl")

    # --- Derived ---
    @property
    def direct_base_url(self) -> str:
        if self.yandex_use_sandbox:
            return "https://api-sandbox.direct.yandex.com/json/v5"
        return "https://api.direct.yandex.com/json/v5"

    @property
    def metrika_base_url(self) -> str:
        return "https://api-metrika.yandex.net"

    @field_validator("audit_log_path")
    @classmethod
    def _ensure_log_dir(cls, v: Path) -> Path:
        v.parent.mkdir(parents=True, exist_ok=True)
        return v

    @field_validator("account_target_cpa_rub")
    @classmethod
    def _reject_non_finite_cpa(cls, v: float | None) -> float | None:
        # ``Field(default=None, gt=0)`` rejects 0 and negative values,
        # but not IEEE-754 specials: ``math.inf > 0`` is True (silently
        # accepted by gt=0), and ``math.nan`` comparison semantics are
        # asymmetric in ways that break rule short-circuits. Reject
        # non-finite explicitly. (auditor M15.5.1 MEDIUM-2.)
        if v is not None and not math.isfinite(v):
            msg = f"account_target_cpa_rub must be a finite positive number, got {v!r}"
            raise ValueError(msg)
        return v

    @field_validator("usd_to_rub_rate", "agent_monthly_llm_budget_rub")
    @classmethod
    def _reject_non_finite_money(cls, v: float | None) -> float | None:
        # Same hardening as ``account_target_cpa_rub``: ``gt=0`` doesn't
        # reject ``inf`` (it satisfies ``inf > 0`` as True). An ``inf``
        # rate would zero out every cost_rub conversion (or in the
        # worst case crash json.dumps in the JSONL store); an ``inf``
        # budget would defeat enforcement when M21.2 lands.
        if v is not None and not math.isfinite(v):
            msg = f"value must be finite, got {v!r}"
            raise ValueError(msg)
        return v


def get_settings() -> Settings:
    """Construct settings. Called from entry points, not import time."""
    return Settings()
