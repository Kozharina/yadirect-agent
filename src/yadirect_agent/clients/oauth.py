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
from urllib.parse import urlencode

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


__all__ = [
    "AUTH_URL",
    "CLIENT_ID",
    "CODE_CHALLENGE_METHOD",
    "REDIRECT_URI",
    "SCOPES",
    "TOKEN_URL",
    "PKCEPair",
    "build_authorization_url",
    "generate_pkce_pair",
]
