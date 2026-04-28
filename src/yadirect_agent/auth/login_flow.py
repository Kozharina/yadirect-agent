"""Yandex OAuth login orchestrator (M15.3).

``perform_login`` is what ``yadirect-agent auth login`` calls. It
ties the four lower layers — PKCE generator, callback server,
token-exchange client, keychain store — into the single user-
visible operation: open browser, click "Разрешить", come back to a
working CLI without ever touching ``.env``.

The orchestration order is rigid because PKCE / CSRF demand it:

1. Generate PKCE pair and a fresh CSRF state.
2. Start the local callback server BEFORE redirecting the browser.
   Otherwise Yandex's redirect arrives at a closed port and the
   operator sees a "connection refused" page with no clear next
   step.
3. Build the auth URL using the just-generated state and challenge.
4. Hand the URL to the operator's browser via ``on_browser_open``
   (defaults to ``webbrowser.open``; CI / headless contexts inject
   a printer instead).
5. Wait for the callback, verifying state and capturing the code.
6. Exchange the code for a TokenSet — using the SAME verifier we
   committed to at step 1, no regeneration in between.
7. Persist the TokenSet to the keychain.
8. Return it.

Tests inject ``pkce``, ``state``, ``callback_port``, and an
``on_browser_open`` hook that drives the local server with a
fabricated callback. Production callers pass nothing — every
default is wired for the real Yandex flow.
"""

from __future__ import annotations

import secrets
import webbrowser
from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from ..clients.oauth import (
    PKCEPair,
    build_authorization_url,
    exchange_code_for_token,
    generate_pkce_pair,
)
from ..models.auth import TokenSet
from .callback_server import LocalCallbackServer
from .keychain import KeyringTokenStore

# Match Yandex's enforced REDIRECT_URI port. ``REDIRECT_URI`` is
# ``http://localhost:8765/callback``; if this knob drifts, every
# production login fails with "redirect_uri mismatch" from Yandex.
# Tests pass an ephemeral port through to dodge collisions on 8765.
DEFAULT_CALLBACK_PORT: int = 8765

# 5 minutes is long enough for a slow 2FA flow but short enough
# that an operator who closed the tab does not block forever.
DEFAULT_LOGIN_TIMEOUT_S: float = 300.0


class _TokenSink(Protocol):
    """Minimum interface the orchestrator needs from the store.

    A Protocol rather than the concrete ``KeyringTokenStore`` so
    tests (and a future M14 multi-account store) can inject any
    object with a ``save(TokenSet)`` method.
    """

    def save(self, token: TokenSet) -> None: ...


def _default_browser_open(url: str) -> None:
    """Open the URL in the operator's default browser.

    ``webbrowser.open`` returns False on headless systems where it
    cannot find a browser binary. We do not retry / print fallback
    here — that judgement belongs to ``cli/auth.py`` which renders
    the URL to stdout when ``open`` reports failure. Keeping the
    orchestrator pure means the same code path drives both
    interactive and headless CLI invocations.
    """
    webbrowser.open(url)


async def perform_login(
    *,
    store: _TokenSink | None = None,
    on_browser_open: Callable[[str], None] | None = None,
    timeout_seconds: float = DEFAULT_LOGIN_TIMEOUT_S,
    callback_port: int = DEFAULT_CALLBACK_PORT,
    pkce: PKCEPair | None = None,
    state: str | None = None,
    now: datetime | None = None,
) -> TokenSet:
    """Run one OAuth login attempt end-to-end.

    Returns the persisted TokenSet on success. Raises on failure:

    - ``OAuthCallbackError`` — user denied, CSRF state mismatch,
      or callback missing ``code``. Operator must restart the
      command and address the underlying cause.
    - ``asyncio.TimeoutError`` — no callback within
      ``timeout_seconds``. Operator probably closed the browser
      tab; restart the command.
    - ``AuthError`` — token exchange rejected (expired code,
      invalid_grant). Restart.
    - ``ApiTransientError`` — transport / 5xx. Restart should
      succeed.
    """
    if store is None:
        store = KeyringTokenStore()
    if on_browser_open is None:
        on_browser_open = _default_browser_open
    if pkce is None:
        pkce = generate_pkce_pair()
    if state is None:
        # 32 bytes = 256 bits of CSRF entropy, base64url-encoded as
        # 43 chars. Same generator we use for the PKCE verifier;
        # both are public-client secrets that need cryptographic
        # randomness.
        state = secrets.token_urlsafe(32)

    server = LocalCallbackServer(expected_state=state, port=callback_port)
    async with server:
        auth_url = build_authorization_url(state=state, code_challenge=pkce.challenge)
        # Hand-off to the browser BEFORE waiting — the local server
        # is already accepting at this point, so even an instant
        # redirect from a cached Yandex session arrives without a
        # race.
        on_browser_open(auth_url)
        code = await server.wait_for_code(timeout_seconds=timeout_seconds)

    token = await exchange_code_for_token(
        code=code,
        code_verifier=pkce.verifier,
        now=now,
    )
    store.save(token)
    return token


__all__ = [
    "DEFAULT_CALLBACK_PORT",
    "DEFAULT_LOGIN_TIMEOUT_S",
    "perform_login",
]
