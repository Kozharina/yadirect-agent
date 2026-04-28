"""Tests for the Yandex OAuth client (M15.3).

This file grows with the OAuth client one TDD pair at a time. Today
it pins:

- The PKCE generator per RFC 7636: verifier length window, charset,
  and the exact ``base64url(sha256(verifier))`` derivation of the
  challenge. PKCE is the security control that lets us be a public
  client (no ``client_secret`` baked in); a regression here silently
  drops us back to the pre-PKCE world where any process on the
  loopback could intercept the auth code.
- The module-level constants (``CLIENT_ID``, ``REDIRECT_URI``,
  ``SCOPES``, ``AUTH_URL``, ``TOKEN_URL``, ``CODE_CHALLENGE_METHOD``).
  These are the contract between the registered OAuth app on Yandex
  and our code; if any of them drifts, the very first ``auth login``
  fails with a generic Yandex error page and the operator has no
  diagnostic.
"""

from __future__ import annotations

import base64
import hashlib
import re

from yadirect_agent.clients.oauth import (
    AUTH_URL,
    CLIENT_ID,
    CODE_CHALLENGE_METHOD,
    REDIRECT_URI,
    SCOPES,
    TOKEN_URL,
    generate_pkce_pair,
)

# RFC 3986 §2.3 unreserved set: ALPHA / DIGIT / "-" / "." / "_" / "~"
_UNRESERVED_RE = re.compile(r"^[A-Za-z0-9\-._~]+$")


class TestPKCE:
    def test_verifier_length_within_rfc7636_window(self) -> None:
        pair = generate_pkce_pair()

        # RFC 7636 §4.1: code_verifier MUST be 43..128 characters.
        # A pair outside this range is rejected by Yandex with a
        # generic 400, leaving the operator without a diagnostic.
        assert 43 <= len(pair.verifier) <= 128

    def test_verifier_uses_only_unreserved_chars(self) -> None:
        pair = generate_pkce_pair()

        assert _UNRESERVED_RE.match(pair.verifier), (
            f"verifier contains characters outside the RFC 7636 charset: {pair.verifier!r}"
        )

    def test_challenge_is_base64url_sha256_of_verifier(self) -> None:
        pair = generate_pkce_pair()

        # The exact derivation per RFC 7636 §4.2:
        # code_challenge = BASE64URL-ENCODE(SHA256(ASCII(code_verifier)))
        # without trailing padding.
        digest = hashlib.sha256(pair.verifier.encode("ascii")).digest()
        expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

        assert pair.challenge == expected

    def test_challenge_has_no_base64_padding(self) -> None:
        pair = generate_pkce_pair()

        # Yandex (per the OAuth 2.1 draft and most providers) rejects
        # padded base64 challenges. The test pins the padding-strip.
        assert "=" not in pair.challenge

    def test_pairs_differ_across_calls(self) -> None:
        # Stochastic smoke: two calls must produce different verifiers.
        # ``secrets.token_urlsafe`` collision in 32 bytes is astronomically
        # unlikely; if this ever fails, the generator has been replaced
        # with a non-random source.
        a = generate_pkce_pair()
        b = generate_pkce_pair()

        assert a.verifier != b.verifier
        assert a.challenge != b.challenge


class TestConstants:
    def test_client_id_matches_registered_yandex_app(self) -> None:
        # Pinned to the OAuth app the operator registered for this
        # project. A drift here produces an opaque Yandex error.
        assert CLIENT_ID == "0e17cd5b7a4d4f1dbc4278626c750260"

    def test_redirect_uri_exact_match(self) -> None:
        # Yandex enforces EXACT-match on redirect_uri (port, path,
        # trailing slash). Even a missing trailing slash drops the
        # auth flow with "redirect_uri mismatch". Pin it.
        assert REDIRECT_URI == "http://localhost:8765/callback"

    def test_scopes_cover_direct_and_metrika(self) -> None:
        # The agent reads Direct (campaigns, keywords, reports) and
        # both reads and writes Metrika (audiences arrive in M9).
        # All three must be in the set or the agent silently 403s
        # on whichever surface is missing.
        assert "direct:api" in SCOPES
        assert "metrika:read" in SCOPES
        assert "metrika:write" in SCOPES

    def test_code_challenge_method_is_s256(self) -> None:
        # ``plain`` reduces PKCE to no protection. We MUST send S256.
        assert CODE_CHALLENGE_METHOD == "S256"

    def test_oauth_endpoints_use_https(self) -> None:
        # Loopback HTTP is fine for the local callback (M15.3 brief),
        # but the OAuth endpoints themselves carry the secret-bearing
        # exchange and MUST be HTTPS. A regression to ``http://`` would
        # leak the access_token in clear over any intercepting hop.
        assert AUTH_URL.startswith("https://")
        assert TOKEN_URL.startswith("https://")
        assert "oauth.yandex.ru" in AUTH_URL
        assert "oauth.yandex.ru" in TOKEN_URL
