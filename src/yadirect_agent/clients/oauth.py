"""Yandex OAuth client (M15.3).

Public OAuth 2.0 PKCE flow client for Yandex's OAuth endpoint
(``oauth.yandex.ru``). We are a public client — no ``client_secret``
ships with the CLI — so PKCE is the security mechanism that makes
the loopback redirect safe: any process on the operator's machine
that intercepts the auth code still cannot exchange it for a token
without the original ``code_verifier``.

Module-level constants are part of the public surface: the login
flow imports them, tests pin them, and they are intentionally
hard-coded rather than env-driven because they are a contract with
the OAuth app registered on Yandex's side. ``CLIENT_ID`` is public
information for any public client and is OK to commit.

This file grows with the OAuth flow one TDD pair at a time. Today
it ships PKCE generation + the public constants. Subsequent commits
add ``build_authorization_url``, ``exchange_code_for_token``, and
``refresh_access_token``.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
from pydantic import SecretStr

from ..exceptions import ApiTransientError, AuthError
from ..models.auth import TokenSet

# --- Module-level constants ---

# Public client ID — registered OAuth app on oauth.yandex.ru. Public
# information for any public client and OK to commit; PKCE makes a
# leaked CLIENT_ID unusable without the matching code_verifier.
CLIENT_ID = "0e17cd5b7a4d4f1dbc4278626c750260"

# Exact-match required by Yandex (port, path, trailing slash are all
# verified server-side). The local one-shot HTTP server in
# ``auth/callback_server.py`` listens on this address.
REDIRECT_URI = "http://localhost:8765/callback"

# OAuth scopes covering everything the agent needs today:
# - ``direct:api``    — campaign / keyword / report read+write on Direct
# - ``metrika:read``  — analytics reads (M6)
# - ``metrika:write`` — audience writes (M9, future)
SCOPES: tuple[str, ...] = ("direct:api", "metrika:read", "metrika:write")

# OAuth endpoints — HTTPS only. The secret-bearing exchange must
# never travel over plain HTTP, even though the loopback callback
# itself does for local convenience.
AUTH_URL = "https://oauth.yandex.ru/authorize"
TOKEN_URL = "https://oauth.yandex.ru/token"  # noqa: S105 — public OAuth endpoint URL, not a secret

# PKCE method. Must be S256: ``plain`` reduces PKCE to no protection.
CODE_CHALLENGE_METHOD = "S256"


# --- PKCE ---


@dataclass(frozen=True, slots=True)
class PKCEPair:
    """A ``code_verifier`` + matching ``code_challenge`` per RFC 7636.

    Both halves are required: the verifier is sent to the token
    endpoint at exchange time, the challenge to the authorization
    endpoint at redirect time. The two values must come from the
    same generation call — mixing them defeats the security.
    """

    verifier: str
    challenge: str


def generate_pkce_pair() -> PKCEPair:
    """Generate a fresh PKCE pair for one OAuth login attempt.

    Uses ``secrets.token_urlsafe(32)`` — 32 bytes is 256 bits of
    entropy, encoded as 43 unreserved characters. The resulting
    verifier sits inside the RFC 7636 [43, 128] window with margin
    to spare. Each call returns an independent pair; callers MUST
    NOT cache or reuse pairs across login attempts.
    """
    verifier = secrets.token_urlsafe(32)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return PKCEPair(verifier=verifier, challenge=challenge)


# --- Authorization URL ---


def build_authorization_url(*, state: str, code_challenge: str) -> str:
    """Build the URL that opens the Yandex OAuth consent page.

    ``state`` is the CSRF defence — a fresh random value per login
    that the local callback server verifies against the value Yandex
    echoes back. ``code_challenge`` is the PKCE half tied to the
    verifier the caller keeps in memory until token exchange.

    Both arguments are validated as non-empty: an empty state silently
    disables CSRF protection (any callback URL would match), and an
    empty challenge silently disables PKCE (the token endpoint would
    accept any verifier or none). We refuse at the builder rather
    than letting either unsafe request go out.
    """
    if not state:
        msg = "state must be a non-empty random string (CSRF defence)"
        raise ValueError(msg)
    if not code_challenge:
        msg = "code_challenge must be non-empty (PKCE protection)"
        raise ValueError(msg)

    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        # Space-separated per OAuth 2.0 §3.3. Yandex silently truncates
        # comma-separated scopes to the first entry — a regression here
        # would leave the agent half-blind without a clear signal.
        "scope": " ".join(SCOPES),
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": CODE_CHALLENGE_METHOD,
    }
    return f"{AUTH_URL}?{urlencode(params)}"


# --- Token exchange ---


# Auth-server status codes that indicate a logical OAuth error
# rather than a transport problem. RFC 6749 §5.2 maps these to the
# ``error_description`` body — we surface that to the operator so
# the CLI can print a useful message rather than "HTTP 400".
_OAUTH_ERROR_STATUSES: frozenset[int] = frozenset({400, 401, 403})

# Per-call timeout for the Yandex OAuth endpoint. 30s is enough for
# Yandex's worst observed latency on token exchange while staying
# below pytest's per-test 10s timeout under normal mocked conditions.
_DEFAULT_OAUTH_TIMEOUT_S: float = 30.0


async def _do_oauth_request(payload: dict[str, str]) -> httpx.Response:
    """Issue the form-encoded POST to the Yandex token endpoint.

    Wrapped in its own helper so ``exchange_code_for_token`` and
    ``refresh_access_token`` share the transport-error mapping
    without a class. The httpx-side timeout (``_DEFAULT_OAUTH_TIMEOUT_S``)
    is passed via the client constructor; an ASYNC109 ``timeout=``
    parameter on the public function would just shadow the same
    knob without adding anything.
    """
    try:
        async with httpx.AsyncClient(timeout=_DEFAULT_OAUTH_TIMEOUT_S) as client:
            return await client.post(TOKEN_URL, data=payload)
    except httpx.TimeoutException as exc:
        msg = f"timeout calling Yandex OAuth token endpoint: {exc}"
        raise ApiTransientError(msg) from exc
    except httpx.TransportError as exc:
        msg = f"transport error against Yandex OAuth: {exc}"
        raise ApiTransientError(msg) from exc


def _raise_for_oauth_error(response: httpx.Response) -> None:
    """Map Yandex OAuth status + body to AuthError / ApiTransientError.

    Splits the auth-vs-transient decision: 4xx (the operator must
    redo ``auth login``) becomes ``AuthError``; everything else
    non-2xx surfaces as ``ApiTransientError`` so the CLI can suggest
    a retry rather than a fix.
    """
    if response.status_code == 200:
        return
    if response.status_code in _OAUTH_ERROR_STATUSES:
        try:
            payload: dict[str, Any] = response.json()
        except ValueError:
            payload = {}
        error = str(payload.get("error", "oauth_error"))
        description = payload.get("error_description")
        message = f"OAuth error: {error}"
        if description:
            message += f" — {description}"
        raise AuthError(message, code=response.status_code)
    msg = f"Yandex OAuth token endpoint returned HTTP {response.status_code}"
    raise ApiTransientError(msg, code=response.status_code)


def _parse_token_payload(payload: dict[str, Any], *, obtained_at: datetime) -> TokenSet:
    """Construct a TokenSet from the parsed JSON body.

    Errors here are server-side regressions or our parser drifting
    from Yandex's actual response shape. We surface them as
    ``ApiTransientError`` so the operator can retry, while keeping
    the original cause for debug logs via ``raise from``.
    """
    try:
        access = payload["access_token"]
        refresh = payload["refresh_token"]
        expires_in = int(payload["expires_in"])
    except (KeyError, TypeError, ValueError) as exc:
        msg = f"malformed token response from Yandex OAuth: {exc}"
        raise ApiTransientError(msg) from exc

    scope_value = payload.get("scope")
    if scope_value:
        # Yandex echoes the GRANTED scope set when it differs from
        # what we requested (user denied a checkbox). Use it
        # verbatim so the agent later does not 403 trying to write
        # to a denied surface.
        scope: tuple[str, ...] = tuple(str(scope_value).split())
    else:
        scope = SCOPES

    expires_at = obtained_at + timedelta(seconds=expires_in)
    return TokenSet(
        access_token=SecretStr(access),
        refresh_token=SecretStr(refresh),
        token_type=str(payload.get("token_type", "bearer")),
        scope=scope,
        obtained_at=obtained_at,
        expires_at=expires_at,
    )


async def exchange_code_for_token(
    *,
    code: str,
    code_verifier: str,
    now: datetime | None = None,
) -> TokenSet:
    """Exchange an authorization code for a TokenSet.

    Called once per ``auth login``: the local callback server hands
    us the ``code`` Yandex sent to our ``REDIRECT_URI``, and the
    ``code_verifier`` that pairs with the ``code_challenge`` we sent
    at authorize time. The verifier proves to Yandex that we are the
    same client that started the flow — which is what makes the
    public-client + loopback-redirect combination secure.

    Not auto-retried: a consumed authorization code cannot be
    replayed. On transient failure, the CLI surfaces "try again" to
    the operator; on auth failure, "redo login".
    """
    obtained_at = now if now is not None else datetime.now(UTC)
    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": code_verifier,
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
    }
    response = await _do_oauth_request(payload)
    _raise_for_oauth_error(response)
    try:
        body: dict[str, Any] = response.json()
    except ValueError as exc:
        msg = f"non-JSON response from Yandex OAuth: {response.text[:200]!r}"
        raise ApiTransientError(msg) from exc
    return _parse_token_payload(body, obtained_at=obtained_at)


__all__ = [
    "AUTH_URL",
    "CLIENT_ID",
    "CODE_CHALLENGE_METHOD",
    "REDIRECT_URI",
    "SCOPES",
    "TOKEN_URL",
    "PKCEPair",
    "build_authorization_url",
    "exchange_code_for_token",
    "generate_pkce_pair",
]
