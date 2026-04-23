"""Shared fixtures for unit tests.

Philosophy:
- Every fixture returns a fresh, independent object — no hidden singleton.
- Secrets are obvious placeholders. Any real token leaking into a fixture
  is a CI failure, not a production leak.
- Event loop policy is left to pytest-asyncio auto mode.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr

from yadirect_agent.config import Settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """A safe, sandbox-scoped Settings instance for tests.

    - Tokens are non-empty so header construction works, but obviously fake.
    - Audit log path is under pytest's tmp_path so tests don't pollute the
      real ./logs directory.
    - Sandbox flag is true — if any test code contacts production, that's
      the real bug.
    """
    return Settings(
        yandex_direct_token=SecretStr("test-direct-token"),
        yandex_metrika_token=SecretStr("test-metrika-token"),
        yandex_client_login=None,
        yandex_use_sandbox=True,
        anthropic_api_key=SecretStr("test-anthropic-key"),
        anthropic_model="claude-opus-4-7",
        agent_policy_path=tmp_path / "agent_policy.yml",
        agent_max_daily_budget_rub=10_000,
        log_level="INFO",
        log_format="json",
        audit_log_path=tmp_path / "logs" / "audit.jsonl",
    )


@pytest.fixture
def settings_with_client_login(tmp_path: Path) -> Settings:
    """Variant with agency Client-Login set — exercises the Use-Operator-Units header branch."""
    return Settings(
        yandex_direct_token=SecretStr("test-direct-token"),
        yandex_metrika_token=SecretStr("test-metrika-token"),
        yandex_client_login="client-sub-account",
        yandex_use_sandbox=True,
        anthropic_api_key=SecretStr("test-anthropic-key"),
        anthropic_model="claude-opus-4-7",
        agent_policy_path=tmp_path / "agent_policy.yml",
        agent_max_daily_budget_rub=10_000,
        log_level="INFO",
        log_format="json",
        audit_log_path=tmp_path / "logs" / "audit.jsonl",
    )
