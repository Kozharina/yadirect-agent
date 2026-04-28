"""Tests for the keyring fallback in ``Settings`` (M15.3 layer 7).

After M15.3, the operator's path of least resistance is
``yadirect-agent auth login`` — which writes a TokenSet to the OS
keychain. ``Settings`` then needs to consume that token. This test
file pins the lookup contract:

- Env-provided token wins when present (legacy path stays
  authoritative; ops can still override by ``YANDEX_DIRECT_TOKEN``
  / ``YANDEX_METRIKA_TOKEN`` env vars in CI / Docker).
- Empty env + keyring token → keyring's ``access_token`` flows into
  both ``yandex_direct_token`` and ``yandex_metrika_token`` (one
  access token, two API surfaces — same OAuth grant covers both).
- Empty env + empty keyring → both tokens stay empty SecretStr
  (existing behavior: Settings boots, read-only paths still work,
  the first authenticated call raises ``AuthError``).
- Settings construction never crashes when the keyring backend is
  unavailable (headless / Docker / CI). Same fail-soft contract
  ``KeyringTokenStore.load`` already enforces.

The fixture-injected ``settings`` from ``tests/unit/conftest.py``
passes non-empty tokens explicitly, so existing tests bypass this
hydration path and continue to work without changes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import keyring.errors
import pytest
from pydantic import SecretStr

from yadirect_agent.auth.keychain import (
    KEYRING_SERVICE_NAME,
    KEYRING_USERNAME,
    KeyringTokenStore,
)
from yadirect_agent.config import Settings
from yadirect_agent.models.auth import TokenSet


@pytest.fixture
def memory_keyring(monkeypatch: pytest.MonkeyPatch) -> dict[tuple[str, str], str]:
    storage: dict[tuple[str, str], str] = {}

    def set_password(service: str, username: str, password: str) -> None:
        storage[(service, username)] = password

    def get_password(service: str, username: str) -> str | None:
        return storage.get((service, username))

    def delete_password(service: str, username: str) -> None:
        key = (service, username)
        if key not in storage:
            raise keyring.errors.PasswordDeleteError(f"no password for {key}")
        del storage[key]

    monkeypatch.setattr("keyring.set_password", set_password)
    monkeypatch.setattr("keyring.get_password", get_password)
    monkeypatch.setattr("keyring.delete_password", delete_password)
    return storage


def _persist_test_token(access: str = "AQAA-from-keyring") -> None:
    now = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
    token = TokenSet(
        access_token=SecretStr(access),
        refresh_token=SecretStr("1.AQAA-refresh"),
        token_type="bearer",
        scope=("direct:api", "metrika:read", "metrika:write"),
        obtained_at=now,
        expires_at=now + timedelta(days=365),
    )
    KeyringTokenStore().save(token)


@pytest.fixture
def isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip token-related env vars so the .env file / shell does not
    leak into the construction Settings sees during these tests."""
    for var in (
        "YANDEX_DIRECT_TOKEN",
        "YANDEX_METRIKA_TOKEN",
        "ANTHROPIC_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    # Disable .env file loading by pointing pydantic-settings at a
    # path that does not exist.
    monkeypatch.setenv("YADIRECT_AGENT_ENV_FILE", "/nonexistent")


class TestEnvWins:
    def test_env_token_overrides_keyring(
        self,
        memory_keyring: dict[tuple[str, str], str],
        isolated_env: None,
    ) -> None:
        # Operator sets env explicitly (CI override path) AND has a
        # keyring token. Env MUST win — otherwise CI runs would
        # silently use a stale local-machine token.
        _persist_test_token(access="AQAA-from-keyring")

        s = Settings(
            yandex_direct_token=SecretStr("env-direct-token"),
            yandex_metrika_token=SecretStr("env-metrika-token"),
        )

        assert s.yandex_direct_token.get_secret_value() == "env-direct-token"
        assert s.yandex_metrika_token.get_secret_value() == "env-metrika-token"


class TestKeyringFallback:
    def test_empty_env_pulls_from_keyring_for_both_tokens(
        self,
        memory_keyring: dict[tuple[str, str], str],
        isolated_env: None,
    ) -> None:
        # Single OAuth grant covers both Direct and Metrika scopes,
        # so the same access_token populates both Settings fields.
        _persist_test_token(access="AQAA-keyring-access")

        s = Settings()  # No tokens passed — should hydrate from keyring.

        assert s.yandex_direct_token.get_secret_value() == "AQAA-keyring-access"
        assert s.yandex_metrika_token.get_secret_value() == "AQAA-keyring-access"

    def test_empty_env_and_empty_keyring_keeps_tokens_empty(
        self,
        memory_keyring: dict[tuple[str, str], str],
        isolated_env: None,
    ) -> None:
        # Pre-login state: Settings boots, read-only CLI paths
        # (``--version``, ``mcp serve``) still work; the first
        # authenticated call raises later with a clearer error.
        s = Settings()  # No tokens, no keyring entry.

        assert s.yandex_direct_token.get_secret_value() == ""
        assert s.yandex_metrika_token.get_secret_value() == ""

    def test_corrupt_keyring_payload_does_not_crash_settings(
        self,
        memory_keyring: dict[tuple[str, str], str],
        isolated_env: None,
    ) -> None:
        # Same fail-soft contract ``KeyringTokenStore.load`` enforces:
        # a bogus payload must not block ``Settings()`` construction.
        # Without this, a corrupt keychain entry would brick every
        # CLI invocation including ``auth revoke`` (the recovery
        # command).
        memory_keyring[(KEYRING_SERVICE_NAME, KEYRING_USERNAME)] = "{not-json"

        s = Settings()

        assert s.yandex_direct_token.get_secret_value() == ""
        assert s.yandex_metrika_token.get_secret_value() == ""

    def test_partial_env_override_keeps_other_keyring(
        self,
        memory_keyring: dict[tuple[str, str], str],
        isolated_env: None,
    ) -> None:
        # ``YANDEX_DIRECT_TOKEN`` is set, ``YANDEX_METRIKA_TOKEN`` is not.
        # Mixed deployments are common (one token in env, one in
        # keyring) — pin the per-field independence.
        _persist_test_token(access="AQAA-keyring-access")

        s = Settings(yandex_direct_token=SecretStr("env-direct-only"))

        assert s.yandex_direct_token.get_secret_value() == "env-direct-only"
        assert s.yandex_metrika_token.get_secret_value() == "AQAA-keyring-access"
