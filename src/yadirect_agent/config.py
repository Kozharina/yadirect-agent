"""Central configuration. Loaded once from env, passed explicitly everywhere.

Design choices:
- pydantic-settings for typed config with validation at startup (fail fast).
- No global singleton — we pass Settings into clients/services via DI so
  tests can swap it out cleanly.
- SecretStr for tokens so they never accidentally end up in logs.
"""

from __future__ import annotations

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


def get_settings() -> Settings:
    """Construct settings. Called from entry points, not import time."""
    return Settings()
