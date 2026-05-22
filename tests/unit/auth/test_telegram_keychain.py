"""Tests for ``KeyringTelegramStore`` (M18 slice 4).

The store is the canonical path Telegram credentials take to the
OS keychain — symmetric to ``KeyringTokenStore`` for OAuth (M15.3).
Slice 4's wizard (``yadirect-agent notify setup telegram``) writes
here; ``Settings._hydrate_tokens_from_keyring`` reads here when
``TELEGRAM_BOT_TOKEN`` / ``TELEGRAM_CHAT_ID`` env-vars are absent.

Why a separate store class instead of reusing ``KeyringTokenStore``:

- The payload shape differs — OAuth holds ``TokenSet`` (4 secret
  fields + scope + timestamps); Telegram holds 2 strings (token,
  chat_id). Forcing a single class to serialise both shapes pushes
  union-types into ``load`` and obscures the per-credential contract.
- Different ``KEYRING_USERNAME`` slot (``"telegram"`` vs ``"oauth"``)
  so deleting one credential set does not affect the other.
  ``yadirect-agent auth logout`` must NOT silently strip Telegram
  setup, and ``yadirect-agent notify setup telegram --reset`` must
  NOT log the operator out of Yandex.

What we pin:

- Round-trip: a ``(token, chat_id)`` pair that goes in via ``save``
  comes out byte-for-byte equal via ``load``. Anything else makes
  the wizard's "setup successful" message a lie.
- Slot constants — operators clear keychain entries by hand on each
  OS; ``KEYRING_TELEGRAM_USERNAME`` is the greppable identifier.
- Defensive ``load``: missing slot, corrupt JSON, missing fields →
  ``None``. Same one-recovery-path collapse as ``KeyringTokenStore``;
  caller (``Settings``) falls back to env-vars or, ultimately, to
  the wizard.
- Idempotent ``delete``: running ``--reset`` twice in a row must not
  raise.

Tests use the same in-memory keyring monkeypatch as the OAuth store
tests — never touches the real OS keychain.
"""

from __future__ import annotations

import keyring.errors
import pytest
from yadirect_agent.auth.telegram_keychain import (
    KEYRING_TELEGRAM_SERVICE_NAME,
    KEYRING_TELEGRAM_USERNAME,
    KeyringTelegramStore,
)


@pytest.fixture
def memory_keyring(monkeypatch: pytest.MonkeyPatch) -> dict[tuple[str, str], str]:
    """Replace the global keyring module with an in-memory dict."""
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


class TestKeyringSlot:
    def test_service_name_matches_project_convention(self) -> None:
        # Same service name as OAuth — one root identifier per project
        # in Keychain Access / Credential Manager. The per-credential
        # slot differentiator is the USERNAME, not the SERVICE.
        assert KEYRING_TELEGRAM_SERVICE_NAME == "yadirect-agent"

    def test_telegram_username_is_distinct_from_oauth(self) -> None:
        # Critical: must NOT collide with the OAuth slot
        # (``KEYRING_USERNAME = "oauth"``), otherwise ``auth logout``
        # would silently wipe Telegram setup. Pin the value so a
        # refactor that consolidates constants cannot accidentally
        # merge them.
        assert KEYRING_TELEGRAM_USERNAME == "telegram"

    def test_telegram_slot_independent_from_oauth_slot(
        self, memory_keyring: dict[tuple[str, str], str]
    ) -> None:
        # Belt-and-braces: even if someone refactors the constants
        # to share a literal, the slot-independence regression
        # surfaces here. Write Telegram creds, then write a fake
        # OAuth payload to the same SERVICE / different USERNAME;
        # Telegram load must still succeed.
        from yadirect_agent.auth.keychain import KEYRING_SERVICE_NAME, KEYRING_USERNAME

        store = KeyringTelegramStore()
        store.save(bot_token="abc:xyz", chat_id="42")

        # Simulate an OAuth credential write to the parallel slot.
        memory_keyring[(KEYRING_SERVICE_NAME, KEYRING_USERNAME)] = '{"fake": "oauth"}'

        loaded = store.load()
        assert loaded is not None
        assert loaded == ("abc:xyz", "42")


class TestRoundTrip:
    def test_save_then_load_returns_equal_pair(
        self, memory_keyring: dict[tuple[str, str], str]
    ) -> None:
        store = KeyringTelegramStore()
        store.save(bot_token="1234567890:ABCdef-real-bot-token", chat_id="987654321")

        loaded = store.load()
        assert loaded is not None
        token, chat_id = loaded
        assert token == "1234567890:ABCdef-real-bot-token"
        assert chat_id == "987654321"

    def test_save_overwrites_previous_value(
        self, memory_keyring: dict[tuple[str, str], str]
    ) -> None:
        # Wizard re-run scenario: operator runs ``notify setup
        # telegram`` again (perhaps they revoked the old bot in
        # @BotFather and made a new one). The second ``save`` must
        # fully replace the first.
        store = KeyringTelegramStore()
        store.save(bot_token="old-token", chat_id="old-chat")
        store.save(bot_token="new-token", chat_id="new-chat")

        assert store.load() == ("new-token", "new-chat")

    def test_save_uses_single_atomic_slot(self, memory_keyring: dict[tuple[str, str], str]) -> None:
        # Single (service, username) pair holds the whole pair as one
        # JSON blob — splitting token and chat_id across separate
        # slots would open a TOCTOU window where a crash between the
        # two writes leaves the keychain inconsistent (token saved,
        # chat_id not). One slot, atomic write.
        store = KeyringTelegramStore()
        store.save(bot_token="t", chat_id="c")

        # Exactly one keyring entry written.
        assert len(memory_keyring) == 1


class TestLoadDefensive:
    def test_load_returns_none_when_slot_empty(
        self, memory_keyring: dict[tuple[str, str], str]
    ) -> None:
        # Fresh install: no keychain entry. Caller (Settings) will
        # fall back to env-vars, then to "feature disabled".
        store = KeyringTelegramStore()
        assert store.load() is None

    def test_load_returns_none_on_corrupt_json(
        self, memory_keyring: dict[tuple[str, str], str]
    ) -> None:
        # Manually edited keychain entry, partial write under
        # power loss — defensive return is "no usable creds", not
        # an exception. Operator re-runs the wizard.
        memory_keyring[(KEYRING_TELEGRAM_SERVICE_NAME, KEYRING_TELEGRAM_USERNAME)] = (
            "{not-valid-json"
        )
        store = KeyringTelegramStore()
        assert store.load() is None

    def test_load_returns_none_when_token_missing(
        self, memory_keyring: dict[tuple[str, str], str]
    ) -> None:
        # JSON parses but lacks the required ``bot_token`` field.
        # Same recovery path as corrupt JSON.
        import json

        memory_keyring[(KEYRING_TELEGRAM_SERVICE_NAME, KEYRING_TELEGRAM_USERNAME)] = json.dumps(
            {"chat_id": "42"}
        )
        store = KeyringTelegramStore()
        assert store.load() is None

    def test_load_returns_none_when_chat_id_missing(
        self, memory_keyring: dict[tuple[str, str], str]
    ) -> None:
        import json

        memory_keyring[(KEYRING_TELEGRAM_SERVICE_NAME, KEYRING_TELEGRAM_USERNAME)] = json.dumps(
            {"bot_token": "t"}
        )
        store = KeyringTelegramStore()
        assert store.load() is None

    def test_load_returns_none_when_backend_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Headless Linux without keyrings.alt, Docker without dbus —
        # the keyring backend itself fails. ``KeyringError`` is the
        # base class for all backend-side issues; catching it keeps
        # us forward-compatible with future keyring versions.
        def raise_keyring_error(*_: object, **__: object) -> str | None:
            raise keyring.errors.NoKeyringError("no backend in CI")

        monkeypatch.setattr("keyring.get_password", raise_keyring_error)
        store = KeyringTelegramStore()
        assert store.load() is None


class TestDelete:
    def test_delete_removes_existing_slot(self, memory_keyring: dict[tuple[str, str], str]) -> None:
        store = KeyringTelegramStore()
        store.save(bot_token="t", chat_id="c")
        store.delete()
        assert store.load() is None

    def test_delete_is_idempotent_on_empty_slot(
        self, memory_keyring: dict[tuple[str, str], str]
    ) -> None:
        # ``notify setup telegram --reset`` called twice in a row, or
        # called on a fresh install — must not raise. Symmetric to
        # ``auth logout``.
        store = KeyringTelegramStore()
        store.delete()  # must not raise
        store.delete()  # second call also no-op


class TestSavingRejectsEmptyValues:
    def test_save_rejects_empty_token(self, memory_keyring: dict[tuple[str, str], str]) -> None:
        # An empty token is a wizard-bug (validation must have caught
        # it earlier). Reject loudly so the wizard's "saved" message
        # cannot be a lie. Symmetric to ``TelegramSink.__init__``'s
        # rejection of empty constructor args.
        store = KeyringTelegramStore()
        with pytest.raises(ValueError, match="bot_token"):
            store.save(bot_token="", chat_id="c")

    def test_save_rejects_empty_chat_id(self, memory_keyring: dict[tuple[str, str], str]) -> None:
        store = KeyringTelegramStore()
        with pytest.raises(ValueError, match="chat_id"):
            store.save(bot_token="t", chat_id="")
