"""Tests for the OAuth login orchestrator (M15.3).

``perform_login`` ties the four lower layers together:

    PKCE pair  → callback server → consent (browser) →
    code exchange → keychain save → return TokenSet

Each layer has its own dedicated tests; this file pins the *wiring*:

- The PKCE verifier we generate is the one we send to
  ``exchange_code_for_token``. A regression that re-generates it
  between authorize and exchange would break PKCE silently.
- The state we put in the auth URL is the one we configure on the
  callback server. CSRF defence depends on the same value being
  enforced server-side and presented client-side.
- The TokenSet returned by exchange is persisted via the store
  before the function returns. ``auth login`` is meaningless if
  the token is dropped on the floor.
- ``on_browser_open`` is invoked exactly once with the auth URL
  so a headless deployment can override it (print to stdout
  instead of opening a window).

Tests use ephemeral ports + injected PKCE/state so multiple test
runs do not collide with the real ``REDIRECT_URI`` port (8765).
``respx`` mocks Yandex's token endpoint; an in-memory keychain
fixture replaces the OS keyring.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
from datetime import UTC, datetime
from typing import Any

import httpx
import keyring.errors
import pytest
import respx

from yadirect_agent.auth.keychain import (
    KEYRING_SERVICE_NAME,
    KEYRING_USERNAME,
    KeyringTokenStore,
)
from yadirect_agent.auth.login_flow import perform_login
from yadirect_agent.clients.oauth import (
    CLIENT_ID,
    REDIRECT_URI,
    TOKEN_URL,
    PKCEPair,
)


def _free_port() -> int:
    """Bind-and-release a free loopback port for tests.

    Tiny race window between close and the test's rebind, but
    acceptable for unit tests; CI is not running competing
    processes against the loopback.
    """
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
    finally:
        s.close()


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


_TEST_STATE = "test-state-csrf-32-bytes-of-randomness"
_TEST_PKCE = PKCEPair(
    verifier="test-verifier-43-chars-XXXXXXXXXXXXXXXXXXXXXX",
    challenge="test-challenge-XYZ",
)
_FROZEN_NOW = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)


def _success_token_payload(*, access: str = "AQAA-test-access") -> dict[str, Any]:
    return {
        "token_type": "bearer",
        "access_token": access,
        "refresh_token": "1.AQAA-test-refresh",
        "expires_in": 31_536_000,
    }


def _drive_callback_when_browser_opens(
    *,
    test_port: int,
    state: str,
    code: str = "test-auth-code",
) -> tuple[Any, list[str]]:
    """Build an ``on_browser_open`` hook that, when the orchestrator
    "opens" the auth URL, schedules a coroutine driving the local
    callback server with a valid callback. Returns the hook plus a
    list that captures the URLs handed to it (for assertions).

    Uses a raw asyncio socket rather than ``httpx`` because the test
    runs under ``@respx.mock``, which intercepts every ``httpx``
    request — including the loopback one we want to actually hit
    our server. Raw sockets bypass that interception."""
    captured: list[str] = []

    async def _drive() -> None:
        # Yield once so perform_login can reach ``wait_for_code``.
        await asyncio.sleep(0.05)
        reader, writer = await asyncio.open_connection("127.0.0.1", test_port)
        try:
            request = (
                f"GET /callback?code={code}&state={state} HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{test_port}\r\n"
                "Connection: close\r\n"
                "\r\n"
            )
            writer.write(request.encode("ascii"))
            await writer.drain()
            # Drain the server's response so the server-side write
            # completes cleanly.
            while await reader.read(1024):
                pass
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    # Hold a strong reference to the task so it cannot be garbage-
    # collected mid-flight (RUF006). The list lives until the test
    # closure goes out of scope.
    pending: list[asyncio.Task[None]] = []

    def hook(url: str) -> None:
        captured.append(url)
        pending.append(asyncio.create_task(_drive()))

    return hook, captured


class TestHappyPath:
    @respx.mock
    async def test_full_flow_returns_and_persists_tokenset(
        self, memory_keyring: dict[tuple[str, str], str]
    ) -> None:
        respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(200, json=_success_token_payload()),
        )
        port = _free_port()
        hook, _captured = _drive_callback_when_browser_opens(test_port=port, state=_TEST_STATE)

        token = await perform_login(
            on_browser_open=hook,
            pkce=_TEST_PKCE,
            state=_TEST_STATE,
            callback_port=port,
            timeout_seconds=2.0,
            now=_FROZEN_NOW,
        )

        # Token returned matches what Yandex sent.
        assert token.access_token.get_secret_value() == "AQAA-test-access"
        # And it landed in the keychain at the pinned slot.
        assert (KEYRING_SERVICE_NAME, KEYRING_USERNAME) in memory_keyring
        # And the keychain contents survive a fresh load.
        loaded = KeyringTokenStore().load()
        assert loaded == token

    @respx.mock
    async def test_pkce_verifier_propagated_to_exchange(
        self, memory_keyring: dict[tuple[str, str], str]
    ) -> None:
        # The verifier in memory at exchange time MUST equal the one
        # whose challenge was sent at authorize time. A regression
        # that re-generates between would mean Yandex rejects the
        # exchange with ``invalid_grant``.
        route = respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(200, json=_success_token_payload()),
        )
        port = _free_port()
        hook, _captured = _drive_callback_when_browser_opens(test_port=port, state=_TEST_STATE)

        await perform_login(
            on_browser_open=hook,
            pkce=_TEST_PKCE,
            state=_TEST_STATE,
            callback_port=port,
            timeout_seconds=2.0,
            now=_FROZEN_NOW,
        )

        request_body = route.calls.last.request.content.decode("ascii")
        assert "code_verifier=test-verifier" in request_body

    @respx.mock
    async def test_browser_hook_receives_auth_url(
        self, memory_keyring: dict[tuple[str, str], str]
    ) -> None:
        respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(200, json=_success_token_payload()),
        )
        port = _free_port()
        hook, captured = _drive_callback_when_browser_opens(test_port=port, state=_TEST_STATE)

        await perform_login(
            on_browser_open=hook,
            pkce=_TEST_PKCE,
            state=_TEST_STATE,
            callback_port=port,
            timeout_seconds=2.0,
            now=_FROZEN_NOW,
        )

        # Exactly one URL handed off — a regression that opens twice
        # would spawn duplicate browser tabs.
        assert len(captured) == 1
        url = captured[0]
        assert "https://oauth.yandex.ru/authorize" in url
        assert f"client_id={CLIENT_ID}" in url
        assert f"state={_TEST_STATE}" in url

    @respx.mock
    async def test_explicit_store_used_when_provided(
        self, memory_keyring: dict[tuple[str, str], str]
    ) -> None:
        respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(200, json=_success_token_payload()),
        )
        port = _free_port()
        hook, _ = _drive_callback_when_browser_opens(test_port=port, state=_TEST_STATE)

        recorded: list[Any] = []

        class _RecordingStore:
            def save(self, token: Any) -> None:
                recorded.append(token)

        await perform_login(
            on_browser_open=hook,
            pkce=_TEST_PKCE,
            state=_TEST_STATE,
            callback_port=port,
            timeout_seconds=2.0,
            now=_FROZEN_NOW,
            store=_RecordingStore(),  # type: ignore[arg-type]
        )

        # Production default uses the real KeyringTokenStore; tests
        # may inject a recorder. Pin the injection point.
        assert len(recorded) == 1


class TestRedirectUriContract:
    def test_redirect_uri_pinned_to_8765(self) -> None:
        # The orchestrator's default callback_port is 8765 because
        # Yandex enforces exact-match on REDIRECT_URI. Drift here
        # would silently break every operator's first login.
        assert REDIRECT_URI.endswith(":8765/callback")
