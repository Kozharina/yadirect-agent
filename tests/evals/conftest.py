"""Fixtures shared across agent evals.

Provides a sandbox-scoped ``Settings`` fixture identical to the
unit-test one (eval directory is a sibling of ``tests/unit/`` so we
can't inherit fixtures via parent conftest discovery). Adds
eval-specific fixtures: a fresh ``FakeDirectService`` per eval and
a helper that patches every ``DirectService`` import site in one
call.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr

from yadirect_agent.config import Settings

from .harness import FakeDirectService, patch_direct_service


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Sandbox-scoped Settings — same shape as
    ``tests/unit/conftest.py::settings`` but re-declared here because
    pytest's conftest discovery does not bridge sibling subtrees.
    """
    return Settings(
        yandex_direct_token=SecretStr("test-direct-token"),
        yandex_metrika_token=SecretStr("test-metrika-token"),
        yandex_client_login=None,
        yandex_use_sandbox=True,
        anthropic_api_key=SecretStr("test-anthropic-key"),
        anthropic_model="claude-opus-4-7",
        agent_policy_path=tmp_path / "agent_policy.yml",
        agent_max_daily_budget_rub=100_000,
        log_level="INFO",
        log_format="json",
        audit_log_path=tmp_path / "logs" / "audit.jsonl",
    )


@pytest.fixture
def fake_direct(monkeypatch: pytest.MonkeyPatch) -> FakeDirectService:
    """Fresh in-memory ``DirectService`` fake patched at every consumer
    site. Tests seed ``fake_direct.campaigns`` / ``fake_direct.keywords``
    before calling the agent.
    """
    fake = FakeDirectService()
    patch_direct_service(monkeypatch, fake)
    return fake
