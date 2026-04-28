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


__all__ = [
    "AUTH_URL",
    "CLIENT_ID",
    "CODE_CHALLENGE_METHOD",
    "REDIRECT_URI",
    "SCOPES",
    "TOKEN_URL",
    "PKCEPair",
    "generate_pkce_pair",
]
