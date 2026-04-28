"""Tests for ``KeyringTokenStore`` (M15.3).

The store is the only path the OAuth token data takes to the OS
keychain: every read goes through ``load``, every write through
``save``, every clear through ``delete``. So the contract this file
pins is the contract the rest of M15.3 — login flow, CLI commands,
Settings keyring fallback — relies on.

What we enforce:

- Round-trip: a TokenSet that goes in via ``save`` comes out
  byte-for-byte equal via ``load``. Anything else silently logs the
  operator out.
- Single-slot semantics: one ``service / username`` pair holds the
  whole TokenSet as one JSON-blob. Splitting access and refresh
  across separate slots would open a TOCTOU window between writes.
- Defensive ``load``: a missing slot returns ``None``; a corrupt
  payload (manually-edited keychain entry, partial write under
  power loss) also returns ``None`` and the operator is back to
  "not logged in" rather than facing an opaque exception.
- ``delete`` is idempotent: calling it on an empty slot is a no-op,
  not an exception, so ``yadirect-agent auth logout`` always exits
  zero on the no-op path.

Tests use an in-memory keyring backend via monkeypatch — never
touches the real OS keychain. (Bringing up a real backend in CI
would be both flaky and dangerous; the only thing that matters
here is the call shape between our code and the keyring module.)
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
from yadirect_agent.models.auth import TokenSet


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


def _ts(*, access: str = "AQAA-access", refresh: str = "1.AQAA-refresh") -> TokenSet:
    now = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
    return TokenSet(
        access_token=SecretStr(access),
        refresh_token=SecretStr(refresh),
        token_type="bearer",
        scope=("direct:api", "metrika:read", "metrika:write"),
        obtained_at=now,
        expires_at=now + timedelta(days=365),
    )


class TestKeyringSlot:
    def test_keyring_service_name_is_pinned(self) -> None:
        # Operators need a stable, greppable identifier when they
        # remove the entry manually (Keychain Access on macOS, etc.).
        # Pin both so a refactor cannot quietly orphan past entries.
        assert KEYRING_SERVICE_NAME == "yadirect-agent"

    def test_keyring_username_is_pinned(self) -> None:
        assert KEYRING_USERNAME == "oauth"


class TestRoundTrip:
    def test_save_then_load_returns_equal_tokenset(
        self, memory_keyring: dict[tuple[str, str], str]
    ) -> None:
        store = KeyringTokenStore()
        original = _ts(access="real-access", refresh="real-refresh")

        store.save(original)
        loaded = store.load()

        assert loaded == original
        assert loaded is not None
        assert loaded.access_token.get_secret_value() == "real-access"
        assert loaded.refresh_token.get_secret_value() == "real-refresh"

    def test_save_uses_pinned_service_and_username(
        self, memory_keyring: dict[tuple[str, str], str]
    ) -> None:
        store = KeyringTokenStore()
        store.save(_ts())

        # Exactly one slot, at the pinned service/username pair.
        assert list(memory_keyring.keys()) == [(KEYRING_SERVICE_NAME, KEYRING_USERNAME)]

    def test_save_overwrites_existing_record(
        self, memory_keyring: dict[tuple[str, str], str]
    ) -> None:
        # ``auth login`` replaces a prior token in-place. A regression
        # that appends instead would corrupt the slot (only one JSON
        # blob fits) and silently log the operator out.
        store = KeyringTokenStore()
        store.save(_ts(access="old"))
        store.save(_ts(access="new"))

        loaded = store.load()
        assert loaded is not None
        assert loaded.access_token.get_secret_value() == "new"


class TestLoadDefensive:
    def test_load_returns_none_when_no_record(
        self, memory_keyring: dict[tuple[str, str], str]
    ) -> None:
        # Fresh-install path: keychain has nothing. ``auth status``
        # must be able to detect "not logged in" without crashing.
        store = KeyringTokenStore()

        assert store.load() is None

    def test_load_returns_none_on_corrupt_json(
        self, memory_keyring: dict[tuple[str, str], str]
    ) -> None:
        # An operator may have edited the keychain entry by hand or
        # a partial write under power loss may have left invalid
        # JSON. Either way: the right answer is "not logged in"
        # plus a warning, not an exception.
        memory_keyring[(KEYRING_SERVICE_NAME, KEYRING_USERNAME)] = "{not-json"
        store = KeyringTokenStore()

        assert store.load() is None

    def test_load_returns_none_on_validation_error(
        self, memory_keyring: dict[tuple[str, str], str]
    ) -> None:
        # JSON that parses but fails our TokenSet schema (missing
        # access_token, naive timestamp, empty scope, ...) lands the
        # same way: "not logged in" + warning, not an exception.
        memory_keyring[(KEYRING_SERVICE_NAME, KEYRING_USERNAME)] = (
            '{"access_token": "x", "refresh_token": "y"}'
        )
        store = KeyringTokenStore()

        assert store.load() is None


class TestDelete:
    def test_delete_clears_existing_record(
        self, memory_keyring: dict[tuple[str, str], str]
    ) -> None:
        store = KeyringTokenStore()
        store.save(_ts())
        assert store.load() is not None

        store.delete()

        assert store.load() is None
        assert (KEYRING_SERVICE_NAME, KEYRING_USERNAME) not in memory_keyring

    def test_delete_is_idempotent_when_no_record(
        self, memory_keyring: dict[tuple[str, str], str]
    ) -> None:
        # ``yadirect-agent auth logout`` on a fresh install must not
        # raise — operator should always exit zero on the no-op path.
        store = KeyringTokenStore()

        store.delete()  # Must not raise.
        store.delete()  # Twice for good measure.

        assert store.load() is None
