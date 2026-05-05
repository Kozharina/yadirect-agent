"""Tests for the shared OAuth token-refresh helper.

The helper is the single point of truth for the keychain →
``oauth.refresh_access_token`` → Settings + httpx-header dance.
``DirectApiClient.call`` and ``MetrikaService._request`` both
delegate to it; their own tests cover the integration path
(401/52 → refresh → retry succeeds). This file covers the helper's
**direct contract**: every branch the Direct + Metrika integration
tests don't touch on their own (both schemes side-by-side, no-
keychain path, no-httpx-client path, refresh-endpoint failure
classes).

Pinning contracts here means a refactor of either client cannot
silently lose helper coverage; conversely, a refactor of the
helper itself catches its own regressions before either client
test surfaces them.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from yadirect_agent.clients import _token_refresh as token_refresh_mod
from yadirect_agent.clients._token_refresh import refresh_settings_token
from yadirect_agent.config import Settings
from yadirect_agent.models.auth import TokenSet


def _make_settings() -> Settings:
    return Settings(
        yandex_direct_token=SecretStr("OLD-direct-token"),
        yandex_metrika_token=SecretStr("OLD-metrika-token"),
        yandex_use_sandbox=True,
    )


def _make_token(*, access: str = "NEW-access", refresh: str = "NEW-refresh") -> TokenSet:
    now = datetime.now(UTC)
    return TokenSet(
        access_token=SecretStr(access),
        refresh_token=SecretStr(refresh),
        token_type="bearer",
        scope=("direct:api", "metrika:read", "metrika:write"),
        obtained_at=now,
        expires_at=now + timedelta(days=365),
    )


class _FakeKeyringStore:
    """In-memory KeyringTokenStore double — same surface, no OS calls."""

    def __init__(self, *, initial: TokenSet | None = None, load_raises: Exception | None = None):
        self._token = initial
        self._load_raises = load_raises
        self.saved: list[TokenSet] = []

    def load(self) -> TokenSet | None:
        if self._load_raises is not None:
            raise self._load_raises
        return self._token

    def save(self, token: TokenSet) -> None:
        self.saved.append(token)
        self._token = token

    def delete(self) -> None:
        self._token = None


@pytest.fixture
def fake_keyring(monkeypatch: pytest.MonkeyPatch) -> _FakeKeyringStore:
    """Patch ``KeyringTokenStore`` to construct an in-memory double.

    The helper imports lazily inside the function, so we patch the
    class symbol on its *origin* module (``auth.keychain``) — the
    helper's ``from ..auth.keychain import KeyringTokenStore`` runs
    each call and resolves the freshly-patched class.
    """
    store = _FakeKeyringStore(initial=_make_token(access="STORED-access", refresh="r"))
    monkeypatch.setattr(
        "yadirect_agent.auth.keychain.KeyringTokenStore",
        lambda: store,
    )
    return store


class TestSuccessPath:
    @pytest.mark.asyncio
    async def test_bearer_scheme_rewrites_authorization_header(
        self,
        fake_keyring: _FakeKeyringStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The Direct path: scheme="Bearer" → header reads
        # "Bearer NEW-access". A regression to the legacy "OAuth
        # ..." literal would silently 401 every Direct call after
        # refresh.
        async def fake_refresh(*, refresh_token: str) -> TokenSet:
            return _make_token(access="NEW-access", refresh="rotated")

        monkeypatch.setattr(token_refresh_mod, "refresh_access_token", fake_refresh)

        settings = _make_settings()
        client = httpx.AsyncClient(headers={"Authorization": "Bearer OLD-access"})
        try:
            ok = await refresh_settings_token(settings, scheme="Bearer", httpx_client=client)
        finally:
            await client.aclose()

        assert ok is True
        assert client.headers["Authorization"] == "Bearer NEW-access"

    @pytest.mark.asyncio
    async def test_oauth_scheme_rewrites_authorization_header(
        self,
        fake_keyring: _FakeKeyringStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The Metrika path: scheme="OAuth" — Yandex Metrika rejects
        # "Bearer" and only honours the legacy "OAuth" prefix.
        async def fake_refresh(*, refresh_token: str) -> TokenSet:
            return _make_token(access="NEW-access", refresh="rotated")

        monkeypatch.setattr(token_refresh_mod, "refresh_access_token", fake_refresh)

        settings = _make_settings()
        client = httpx.AsyncClient(headers={"Authorization": "OAuth OLD-access"})
        try:
            ok = await refresh_settings_token(settings, scheme="OAuth", httpx_client=client)
        finally:
            await client.aclose()

        assert ok is True
        assert client.headers["Authorization"] == "OAuth NEW-access"

    @pytest.mark.asyncio
    async def test_both_settings_token_fields_mirror_new_access_token(
        self,
        fake_keyring: _FakeKeyringStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # M15.3 contract: a single OAuth grant covers both Direct and
        # Metrika scopes (see Settings._hydrate_tokens_from_keyring).
        # The refresh helper must keep both Settings fields aligned
        # — a regression that updated only the scheme-matching field
        # would silently leave the other client on a stale token.
        async def fake_refresh(*, refresh_token: str) -> TokenSet:
            return _make_token(access="NEW-access", refresh="rotated")

        monkeypatch.setattr(token_refresh_mod, "refresh_access_token", fake_refresh)

        settings = _make_settings()
        ok = await refresh_settings_token(settings, scheme="Bearer")

        assert ok is True
        assert settings.yandex_direct_token.get_secret_value() == "NEW-access"
        assert settings.yandex_metrika_token.get_secret_value() == "NEW-access"

    @pytest.mark.asyncio
    async def test_new_token_persisted_to_keychain(
        self,
        fake_keyring: _FakeKeyringStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The next process invocation must see the fresh token —
        # otherwise every cron run re-refreshes from the same stale
        # in-keychain refresh_token until it expires.
        async def fake_refresh(*, refresh_token: str) -> TokenSet:
            return _make_token(access="NEW-access", refresh="ROTATED-refresh")

        monkeypatch.setattr(token_refresh_mod, "refresh_access_token", fake_refresh)

        settings = _make_settings()
        await refresh_settings_token(settings, scheme="Bearer")

        assert len(fake_keyring.saved) == 1
        assert fake_keyring.saved[0].access_token.get_secret_value() == "NEW-access"
        assert fake_keyring.saved[0].refresh_token.get_secret_value() == "ROTATED-refresh"

    @pytest.mark.asyncio
    async def test_no_httpx_client_skips_header_rewrite(
        self,
        fake_keyring: _FakeKeyringStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Service-level retry loops that re-instantiate per attempt
        # (or unit tests that never spin up a client) pass
        # ``httpx_client=None``. The helper must still update Settings
        # and persist the refreshed token — only the in-process header
        # rewrite is skipped.
        async def fake_refresh(*, refresh_token: str) -> TokenSet:
            return _make_token(access="NEW-access", refresh="rotated")

        monkeypatch.setattr(token_refresh_mod, "refresh_access_token", fake_refresh)

        settings = _make_settings()
        ok = await refresh_settings_token(settings, scheme="Bearer", httpx_client=None)

        assert ok is True
        assert settings.yandex_direct_token.get_secret_value() == "NEW-access"
        assert len(fake_keyring.saved) == 1


class TestFailurePaths:
    @pytest.mark.asyncio
    async def test_no_keychain_token_returns_false(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Operator never ran ``auth login``: keychain returns None.
        # Helper returns False; caller surfaces its original
        # AuthError so the operator sees the actionable cause.
        empty = _FakeKeyringStore(initial=None)
        monkeypatch.setattr(
            "yadirect_agent.auth.keychain.KeyringTokenStore",
            lambda: empty,
        )

        settings = _make_settings()
        ok = await refresh_settings_token(settings, scheme="Bearer")

        assert ok is False
        # Settings stayed on the original tokens — refresh did not fire.
        assert settings.yandex_direct_token.get_secret_value() == "OLD-direct-token"

    @pytest.mark.asyncio
    async def test_keychain_load_failure_returns_false(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # ``KeyringTokenStore.load`` already swallows the documented
        # exception classes (KeyringError, JSONDecodeError,
        # ValidationError). The helper double-guards against
        # future-novel exception classes the backend might surface;
        # without this guard they'd bubble out of the auth refresh
        # path and mask the original AuthError.
        broken = _FakeKeyringStore(load_raises=RuntimeError("future-novel keyring error"))
        monkeypatch.setattr(
            "yadirect_agent.auth.keychain.KeyringTokenStore",
            lambda: broken,
        )

        settings = _make_settings()
        ok = await refresh_settings_token(settings, scheme="Bearer")

        assert ok is False
        assert settings.yandex_direct_token.get_secret_value() == "OLD-direct-token"

    @pytest.mark.asyncio
    async def test_refresh_endpoint_failure_returns_false(
        self,
        fake_keyring: _FakeKeyringStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Refresh token revoked at yandex.ru/profile/access, transient
        # network blip, backend down — all surface as exceptions from
        # ``oauth.refresh_access_token``. None of them merit hiding the
        # original wire AuthError; helper returns False so the caller
        # falls through to the actionable original error.
        async def failing_refresh(*, refresh_token: str) -> Any:
            raise RuntimeError("refresh token rejected")

        monkeypatch.setattr(token_refresh_mod, "refresh_access_token", failing_refresh)

        settings = _make_settings()
        ok = await refresh_settings_token(settings, scheme="Bearer")

        assert ok is False
        # Settings + keychain unchanged on failure.
        assert settings.yandex_direct_token.get_secret_value() == "OLD-direct-token"
        assert fake_keyring.saved == []
