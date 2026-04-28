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
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

from yadirect_agent.clients.oauth import (
    AUTH_URL,
    CLIENT_ID,
    CODE_CHALLENGE_METHOD,
    REDIRECT_URI,
    SCOPES,
    TOKEN_URL,
    build_authorization_url,
    exchange_code_for_token,
    generate_pkce_pair,
)
from yadirect_agent.exceptions import ApiTransientError, AuthError

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


class TestBuildAuthorizationURL:
    def _build(self, *, state: str = "csrf-32-chars-of-random-bytes-XYZ") -> str:
        pair = generate_pkce_pair()
        return build_authorization_url(state=state, code_challenge=pair.challenge)

    def test_url_starts_with_oauth_endpoint(self) -> None:
        url = self._build()

        assert url.startswith(AUTH_URL + "?"), (
            f"URL must extend the AUTH_URL with a query string, got: {url}"
        )

    def test_required_params_present(self) -> None:
        url = self._build()
        params = parse_qs(urlparse(url).query)

        # Yandex enforces presence of every parameter; missing any is a
        # generic 400 with no diagnostic. Pin the full required set.
        assert params["response_type"] == ["code"]
        assert params["client_id"] == [CLIENT_ID]
        assert params["redirect_uri"] == [REDIRECT_URI]
        assert params["code_challenge_method"] == [CODE_CHALLENGE_METHOD]
        assert "code_challenge" in params
        assert "state" in params
        assert "scope" in params

    def test_scope_is_space_separated_per_oauth_spec(self) -> None:
        # OAuth 2.0 §3.3: scope is a list of space-delimited values.
        # Yandex is strict here — comma-separated scopes are silently
        # truncated to the first entry, leaving the agent half-blind.
        url = self._build()
        params = parse_qs(urlparse(url).query)

        scope_value = params["scope"][0]
        assert scope_value == " ".join(SCOPES)

    def test_state_is_propagated_verbatim(self) -> None:
        marker = "csrf-32-chars-of-random-bytes-XYZ"
        url = build_authorization_url(state=marker, code_challenge="abc-challenge")
        params = parse_qs(urlparse(url).query)

        # ``state`` is the CSRF defence: any drift between what we
        # send and what we receive in the callback aborts the login.
        assert params["state"] == [marker]

    def test_code_challenge_is_propagated_verbatim(self) -> None:
        url = build_authorization_url(state="anything", code_challenge="my-challenge-xyz")
        params = parse_qs(urlparse(url).query)

        # The verifier-challenge link breaks if the challenge in the
        # URL differs from the one tied to the verifier we keep in
        # memory — Yandex would 400 the eventual exchange.
        assert params["code_challenge"] == ["my-challenge-xyz"]

    def test_empty_state_rejected(self) -> None:
        # Empty state is no state — eliminates CSRF protection. The
        # caller MUST supply a fresh random value per login.
        with pytest.raises(ValueError, match="state"):
            build_authorization_url(state="", code_challenge="any-challenge")

    def test_empty_challenge_rejected(self) -> None:
        # An empty challenge means PKCE is effectively disabled (the
        # token endpoint will accept any verifier or none). Refuse at
        # the URL builder rather than letting the request go out.
        with pytest.raises(ValueError, match="code_challenge"):
            build_authorization_url(state="any-state", code_challenge="")


_FROZEN_NOW = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)


def _success_payload(
    *,
    expires_in: int = 31_536_000,
    scope: str | None = None,
    refresh: str = "1.AQAA-refresh-token-value",
    access: str = "AQAA-access-token-value",
) -> dict[str, object]:
    body: dict[str, object] = {
        "token_type": "bearer",
        "access_token": access,
        "refresh_token": refresh,
        "expires_in": expires_in,
    }
    if scope is not None:
        body["scope"] = scope
    return body


class TestExchangeCodeForToken:
    @respx.mock
    async def test_success_returns_tokenset_with_correct_fields(self) -> None:
        respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=_success_payload()))

        ts = await exchange_code_for_token(
            code="auth-code-from-callback",
            code_verifier="verifier-43-chars-of-random-bytes-XYZ",
            now=_FROZEN_NOW,
        )

        assert ts.access_token.get_secret_value() == "AQAA-access-token-value"
        assert ts.refresh_token.get_secret_value() == "1.AQAA-refresh-token-value"
        assert ts.token_type == "bearer"
        assert ts.obtained_at == _FROZEN_NOW
        assert ts.expires_at == _FROZEN_NOW + timedelta(seconds=31_536_000)

    @respx.mock
    async def test_request_uses_https_token_url(self) -> None:
        # Defence-in-depth: the secret-bearing exchange must hit the
        # HTTPS endpoint we pinned. respx's ``called`` accessor lets
        # us verify post-hoc that the actual hop was the expected one.
        route = respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(200, json=_success_payload())
        )

        await exchange_code_for_token(
            code="auth-code",
            code_verifier="some-verifier",
            now=_FROZEN_NOW,
        )

        assert route.called
        request = route.calls.last.request
        assert request.url.scheme == "https"
        assert str(request.url) == TOKEN_URL

    @respx.mock
    async def test_request_carries_pkce_verifier_and_grant_type(self) -> None:
        # Pin the form-encoded body. A regression that drops
        # ``code_verifier`` would silently degrade us to the pre-PKCE
        # world (Yandex would still 200 a non-PKCE flow on a public
        # client without checking).
        route = respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(200, json=_success_payload())
        )

        await exchange_code_for_token(
            code="the-code",
            code_verifier="the-verifier",
            now=_FROZEN_NOW,
        )

        request = route.calls.last.request
        assert request.headers["content-type"].startswith("application/x-www-form-urlencoded")
        body = parse_qs(request.content.decode("ascii"))
        assert body["grant_type"] == ["authorization_code"]
        assert body["code"] == ["the-code"]
        assert body["code_verifier"] == ["the-verifier"]
        assert body["client_id"] == [CLIENT_ID]
        assert body["redirect_uri"] == [REDIRECT_URI]

    @respx.mock
    async def test_response_scope_string_overrides_default_scopes(self) -> None:
        # If Yandex narrows the granted scopes (user denied one of the
        # checkboxes), the token has fewer privileges. We MUST surface
        # the actually-granted scope set, not our request's intent —
        # otherwise the agent would later 403 trying to write to a
        # resource the user explicitly denied.
        respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(
                200, json=_success_payload(scope="direct:api metrika:read")
            ),
        )

        ts = await exchange_code_for_token(
            code="c",
            code_verifier="v",
            now=_FROZEN_NOW,
        )

        assert ts.scope == ("direct:api", "metrika:read")

    @respx.mock
    async def test_missing_scope_in_response_falls_back_to_requested(self) -> None:
        # Yandex omits ``scope`` in the response when ALL requested
        # scopes were granted. Falling back to our SCOPES is correct
        # here — pinning that behaviour explicitly.
        respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=_success_payload()))

        ts = await exchange_code_for_token(
            code="c",
            code_verifier="v",
            now=_FROZEN_NOW,
        )

        assert ts.scope == SCOPES

    @respx.mock
    async def test_400_raises_autherror(self) -> None:
        # The most common 400 here is ``invalid_grant`` (expired or
        # already-consumed code, or PKCE verifier mismatch). All four
        # flavours mean the operator must redo ``auth login`` — not
        # retry. AuthError is the right escalation.
        respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(
                400,
                json={"error": "invalid_grant", "error_description": "expired code"},
            ),
        )

        with pytest.raises(AuthError, match="invalid_grant"):
            await exchange_code_for_token(code="c", code_verifier="v", now=_FROZEN_NOW)

    @respx.mock
    async def test_401_raises_autherror(self) -> None:
        # Per RFC 6749 §5.2, ``invalid_client`` returns 401 with a
        # WWW-Authenticate header. AuthError is correct.
        respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(401, json={"error": "invalid_client"}),
        )

        with pytest.raises(AuthError):
            await exchange_code_for_token(code="c", code_verifier="v", now=_FROZEN_NOW)

    @respx.mock
    async def test_500_raises_transient(self) -> None:
        # Token exchange is NOT auto-retried (a consumed code cannot
        # be replayed), but a 5xx surfaces as ApiTransientError so the
        # CLI can tell the operator "try again" rather than "fix your
        # credentials".
        respx.post(TOKEN_URL).mock(return_value=httpx.Response(503, text="bad gateway"))

        with pytest.raises(ApiTransientError):
            await exchange_code_for_token(code="c", code_verifier="v", now=_FROZEN_NOW)

    @respx.mock
    async def test_network_timeout_raises_transient(self) -> None:
        respx.post(TOKEN_URL).mock(side_effect=httpx.TimeoutException("slow"))

        with pytest.raises(ApiTransientError):
            await exchange_code_for_token(code="c", code_verifier="v", now=_FROZEN_NOW)
