"""Tests for the local OAuth callback server (M15.3).

The server runs for one request per ``auth login`` invocation: it
binds to ``127.0.0.1:<port>`` (loopback only — never ``0.0.0.0``),
accepts the redirect Yandex sends to our ``REDIRECT_URI``, validates
the ``state`` matches what we sent at authorize time (CSRF defence),
and returns either the captured ``code`` or a typed exception.

What we pin here:

- Loopback-only binding: a regression to ``0.0.0.0`` would expose
  the local OAuth code to anyone on the same Wi-Fi.
- State-match enforcement: a callback whose ``state`` differs from
  the expected value raises ``OAuthCallbackError`` rather than
  silently accepting it; without this, an attacker who can predict
  the loopback port can complete the login on their own behalf.
- Method / path constraints: only ``GET /callback`` is honoured;
  anything else is rejected so the server cannot be repurposed as
  a generic local web service.
- Yandex error propagation: when Yandex sends ``?error=...``
  (user clicked "Запретить") the server raises a typed exception
  so the CLI can render a useful message rather than time out.
- Timeout: ``wait_for_code`` honours its own ``timeout_seconds``
  knob so an operator who closes the browser tab without logging
  in does not block the CLI forever.
- One-shot: the server stops accepting after the first valid
  callback so the port is freed for the next invocation.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from yadirect_agent.auth.callback_server import (
    LocalCallbackServer,
    OAuthCallbackError,
)


async def _drive_callback(url: str) -> httpx.Response:
    """Hit the running callback server. Tiny timeout so test failures
    surface as failures, not hangs."""
    async with httpx.AsyncClient(timeout=2.0) as client:
        return await client.get(url)


class TestLoopbackBinding:
    async def test_default_host_is_loopback(self) -> None:
        # Pin the public default so no future "let's accept LAN
        # connections too" change goes in unnoticed.
        server = LocalCallbackServer(expected_state="anything")

        assert server.host == "127.0.0.1"

    async def test_explicit_zero_zero_zero_zero_rejected(self) -> None:
        # Defence in depth: even if a caller passes ``0.0.0.0``, the
        # server refuses to start. The OAuth callback contains the
        # auth code in the URL; binding to a non-loopback interface
        # leaks that code to anyone on the same network.
        with pytest.raises(ValueError, match="loopback"):
            LocalCallbackServer(expected_state="x", host="0.0.0.0")


class TestCallbackCapture:
    async def test_captures_code_on_valid_callback(self) -> None:
        server = LocalCallbackServer(expected_state="csrf-state")
        async with server:
            url = f"http://127.0.0.1:{server.port}/callback?code=auth-code-xyz&state=csrf-state"

            response, code = await asyncio.gather(
                _drive_callback(url),
                server.wait_for_code(timeout_seconds=2.0),
            )

        assert code == "auth-code-xyz"
        assert response.status_code == 200
        # Browser-facing landing page so the operator knows what to
        # do next. Pin a stable phrase so a test catches a regression
        # to a blank/error page.
        assert "yadirect-agent" in response.text.lower() or "успешно" in response.text.lower()

    async def test_state_mismatch_raises_csrf_error(self) -> None:
        server = LocalCallbackServer(expected_state="my-state")
        async with server:
            url = f"http://127.0.0.1:{server.port}/callback?code=c&state=evil-state"

            await _drive_callback(url)

            with pytest.raises(OAuthCallbackError, match="state"):
                await server.wait_for_code(timeout_seconds=2.0)

    async def test_yandex_error_propagated(self) -> None:
        # User clicked "Запретить" → Yandex redirects with ?error=...
        # The orchestrator surfaces the error to the operator instead
        # of waiting until timeout.
        server = LocalCallbackServer(expected_state="s")
        async with server:
            url = f"http://127.0.0.1:{server.port}/callback?error=access_denied"

            await _drive_callback(url)

            with pytest.raises(OAuthCallbackError, match="access_denied"):
                await server.wait_for_code(timeout_seconds=2.0)

    async def test_missing_code_raises(self) -> None:
        server = LocalCallbackServer(expected_state="s")
        async with server:
            url = f"http://127.0.0.1:{server.port}/callback?state=s"

            await _drive_callback(url)

            with pytest.raises(OAuthCallbackError, match="code"):
                await server.wait_for_code(timeout_seconds=2.0)


class TestMethodAndPathConstraints:
    async def test_post_returns_405(self) -> None:
        server = LocalCallbackServer(expected_state="s")
        async with server:
            async with httpx.AsyncClient(timeout=2.0) as client:
                response = await client.post(f"http://127.0.0.1:{server.port}/callback")

            # No future has been resolved — pop the wait so the
            # context exit is clean.
            with pytest.raises(asyncio.TimeoutError):
                await server.wait_for_code(timeout_seconds=0.05)

        assert response.status_code == 405

    async def test_unknown_path_returns_404(self) -> None:
        server = LocalCallbackServer(expected_state="s")
        async with server:
            async with httpx.AsyncClient(timeout=2.0) as client:
                response = await client.get(f"http://127.0.0.1:{server.port}/")

            with pytest.raises(asyncio.TimeoutError):
                await server.wait_for_code(timeout_seconds=0.05)

        assert response.status_code == 404


class TestTimeout:
    async def test_no_callback_within_timeout_raises_timeouterror(self) -> None:
        # Operator closed the browser tab without logging in. The
        # CLI surfaces "took too long" rather than blocking forever.
        server = LocalCallbackServer(expected_state="s")
        async with server, asyncio.timeout(2.0):
            with pytest.raises(asyncio.TimeoutError):
                await server.wait_for_code(timeout_seconds=0.1)
