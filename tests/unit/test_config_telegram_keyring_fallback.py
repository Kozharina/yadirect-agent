"""Tests for Telegram-creds keyring fallback in ``Settings`` (M18 slice 4).

Symmetric to ``test_config_keyring_fallback.py`` for OAuth, but for
the ``telegram_bot_token`` / ``telegram_chat_id`` pair landed in
slice 4 alongside the ``notify setup telegram`` wizard.

Pinned contracts:

- Env-provided values win when present (CI / Docker override
  path — operator can still set ``TELEGRAM_BOT_TOKEN`` /
  ``TELEGRAM_CHAT_ID`` to bypass the keychain).
- Empty env + keyring entry → both fields hydrate from the
  keychain. The wizard's "saved successfully" message becomes a
  lie without this.
- Empty env + empty keyring → both fields stay ``None``. The
  ``TelegramSink.from_settings`` returns None on this state, and
  the M18 slice 5a dispatcher Dispatcher.from_settings returns an
  empty Dispatcher (gracefully disabled).
- Settings construction never crashes when the keyring backend is
  unavailable. Same fail-soft contract OAuth keyring hydration
  already enforces — without this, a fresh Docker / headless CI
  install would brick every ``yadirect-agent`` invocation,
  including the wizard itself (which is the recovery path).
- Per-field independence: env provides ONE of bot_token /
  chat_id, keyring provides the OTHER → both end up populated.
  Mixed deployments are normal (env-injected token from a CI
  secret store, chat_id from wizard) and must work.

The fixture-injected ``settings`` from ``tests/unit/conftest.py``
passes ``telegram_bot_token=None`` (default) so existing tests
bypass this hydration path and continue to work without changes.
"""

from __future__ import annotations

import json

import keyring.errors
import pytest
from pydantic import SecretStr

from yadirect_agent.auth.telegram_keychain import (
    KEYRING_TELEGRAM_SERVICE_NAME,
    KEYRING_TELEGRAM_USERNAME,
    KeyringTelegramStore,
)
from yadirect_agent.config import Settings


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


@pytest.fixture
def isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip Telegram + OAuth env vars so Settings construction
    sees only what the test deliberately sets."""
    for var in (
        "YANDEX_DIRECT_TOKEN",
        "YANDEX_METRIKA_TOKEN",
        "ANTHROPIC_API_KEY",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
    ):
        monkeypatch.delenv(var, raising=False)
    # Suppress reading the project .env so a developer's local
    # values don't poison test isolation. Same shape as the
    # M18 slice 1 ``TestTelegramEnvVarIntegration`` tests.
    monkeypatch.setattr(
        "yadirect_agent.config.Settings.model_config",
        {"env_file": None, "extra": "ignore"},
    )


class TestEnvWins:
    def test_env_token_overrides_keyring(
        self,
        memory_keyring: dict[tuple[str, str], str],
        isolated_env: None,
    ) -> None:
        # Operator sets both env vars explicitly AND has a keychain
        # entry from a prior wizard run. Env MUST win — otherwise
        # a CI override would silently use a stale local-machine
        # token.
        KeyringTelegramStore().save(bot_token="keyring-token", chat_id="keyring-chat")

        s = Settings(
            telegram_bot_token=SecretStr("env-token"),
            telegram_chat_id="env-chat",
        )

        assert s.telegram_bot_token is not None
        assert s.telegram_bot_token.get_secret_value() == "env-token"
        assert s.telegram_chat_id == "env-chat"


class TestKeyringFallback:
    def test_empty_env_pulls_both_fields_from_keyring(
        self,
        memory_keyring: dict[tuple[str, str], str],
        isolated_env: None,
    ) -> None:
        # Standard wizard-then-use path: operator ran ``notify setup
        # telegram`` once; subsequent ``yadirect-agent health`` runs
        # find the creds in the keychain without any env-var setup.
        # This is the entire point of slice 4.
        KeyringTelegramStore().save(
            bot_token="1234:ABC-real-bot-token",
            chat_id="987654321",
        )

        s = Settings()

        assert s.telegram_bot_token is not None
        assert s.telegram_bot_token.get_secret_value() == "1234:ABC-real-bot-token"
        assert s.telegram_chat_id == "987654321"

    def test_empty_env_and_empty_keyring_keeps_fields_none(
        self,
        memory_keyring: dict[tuple[str, str], str],
        isolated_env: None,
    ) -> None:
        # Pre-wizard state on a fresh install: Settings boots, the
        # ``health`` CLI works, ``TelegramSink.from_settings`` returns
        # None, the dispatcher is empty + silent. No crash.
        s = Settings()

        assert s.telegram_bot_token is None
        assert s.telegram_chat_id is None

    def test_corrupt_keyring_payload_does_not_crash_settings(
        self,
        memory_keyring: dict[tuple[str, str], str],
        isolated_env: None,
    ) -> None:
        # Manually edited keychain entry, partial write under power
        # loss — fall back to "no creds", do not block Settings()
        # construction (which would brick every CLI invocation
        # including the wizard, the recovery path).
        memory_keyring[(KEYRING_TELEGRAM_SERVICE_NAME, KEYRING_TELEGRAM_USERNAME)] = (
            "{not-valid-json"
        )

        s = Settings()

        assert s.telegram_bot_token is None
        assert s.telegram_chat_id is None

    def test_keyring_backend_unavailable_does_not_crash(
        self,
        monkeypatch: pytest.MonkeyPatch,
        isolated_env: None,
    ) -> None:
        # Headless / Docker / CI fail-soft: ``keyring.get_password``
        # raises (no backend); Settings still boots with empty
        # Telegram fields. The OAuth keyring hydration enforces the
        # same fail-soft already; pin it for Telegram too.
        def raise_keyring_error(*_: object, **__: object) -> str | None:
            raise keyring.errors.NoKeyringError("no backend")

        monkeypatch.setattr("keyring.get_password", raise_keyring_error)

        s = Settings()

        assert s.telegram_bot_token is None
        assert s.telegram_chat_id is None

    def test_partial_env_override_keeps_other_keyring(
        self,
        memory_keyring: dict[tuple[str, str], str],
        isolated_env: None,
    ) -> None:
        # Mixed deployment: ``TELEGRAM_BOT_TOKEN`` from CI secrets,
        # ``TELEGRAM_CHAT_ID`` from wizard. Both must end up populated.
        # Per-field independence is what makes "Docker container
        # gets the token from env, the chat_id from a wizard run
        # on the host's keychain via mounted dbus" a viable
        # operator setup. (Not a recommended config, but a working
        # one — the fields are independent.)
        KeyringTelegramStore().save(
            bot_token="keyring-token",
            chat_id="keyring-chat",
        )

        s = Settings(telegram_bot_token=SecretStr("env-only-token"))

        assert s.telegram_bot_token is not None
        assert s.telegram_bot_token.get_secret_value() == "env-only-token"
        # chat_id absent from env → keyring fills it.
        assert s.telegram_chat_id == "keyring-chat"

    def test_keyring_with_oauth_grant_does_not_pollute_telegram_fields(
        self,
        memory_keyring: dict[tuple[str, str], str],
        isolated_env: None,
    ) -> None:
        # OAuth keychain entry exists (separate slot), but no Telegram
        # entry. Telegram fields must stay None — the two stores are
        # independent. Pin against a refactor that collapses the
        # hydration into one keyring read on the wrong slot.
        from yadirect_agent.auth.keychain import KEYRING_SERVICE_NAME, KEYRING_USERNAME

        memory_keyring[(KEYRING_SERVICE_NAME, KEYRING_USERNAME)] = json.dumps(
            {"fake": "oauth-blob"}
        )

        s = Settings()

        assert s.telegram_bot_token is None
        assert s.telegram_chat_id is None
