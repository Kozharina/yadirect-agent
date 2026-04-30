"""Tests for DirectApiClient + UnitsInfo.

Coverage targets (see docs/TESTING.md):
- Happy path: 200 with `result`, Units parsed.
- Error-code mapping: 200 with `{"error": ...}` → typed exceptions.
- Transient retry: 500 → retried; success on subsequent attempt.
- Transient retry: timeout → retried.
- Rate-limit retry: RateLimitError retried by the decorator.
- Header semantics: Client-Login triggers Use-Operator-Units.
- UnitsInfo parsing edge cases.
- Non-JSON body → ApiTransientError.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr

from yadirect_agent.clients.base import DirectApiClient, UnitsInfo
from yadirect_agent.config import Settings
from yadirect_agent.exceptions import (
    ApiTransientError,
    AuthError,
    QuotaExceededError,
    ValidationError,
    YaDirectError,
)
from yadirect_agent.models.auth import TokenSet

# --------------------------------------------------------------------------
# UnitsInfo: pure logic, no network.
# --------------------------------------------------------------------------


class TestUnitsInfo:
    def test_parses_well_formed_header(self) -> None:
        u = UnitsInfo.parse("10/23750/24000")

        assert u is not None
        assert u.last_cost == 10
        assert u.remaining == 23750
        assert u.daily_limit == 24000

    def test_computes_percent_used(self) -> None:
        u = UnitsInfo.parse("10/18000/24000")
        assert u is not None
        # 1 - 18000/24000 = 0.25
        assert u.pct_used == pytest.approx(0.25)

    def test_pct_used_is_zero_when_daily_limit_is_zero(self) -> None:
        u = UnitsInfo(last_cost=0, remaining=0, daily_limit=0)
        assert u.pct_used == 0.0

    def test_returns_none_for_missing_header(self) -> None:
        assert UnitsInfo.parse(None) is None
        assert UnitsInfo.parse("") is None

    def test_returns_none_for_malformed_header(self) -> None:
        assert UnitsInfo.parse("not-a-units-header") is None
        assert UnitsInfo.parse("10/20") is None  # too few parts
        assert UnitsInfo.parse("a/b/c") is None  # non-numeric


# --------------------------------------------------------------------------
# Happy path & header construction.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_call_returns_result_and_parses_units(
    settings: Settings, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.post("https://api-sandbox.direct.yandex.com/json/v5/campaigns").mock(
        return_value=httpx.Response(
            200,
            headers={"Units": "10/23750/24000"},
            json={"result": {"Campaigns": [{"Id": 1, "Name": "c1"}]}},
        )
    )

    async with DirectApiClient(settings) as api:
        result = await api.call("campaigns", "get", {"SelectionCriteria": {}})

    assert route.called
    assert result == {"Campaigns": [{"Id": 1, "Name": "c1"}]}

    request = route.calls[0].request
    assert request.headers["Authorization"] == "Bearer test-direct-token"
    assert request.headers["Accept-Language"] == "ru"
    assert request.headers["Content-Type"].startswith("application/json")
    assert "Client-Login" not in request.headers
    assert "Use-Operator-Units" not in request.headers


@pytest.mark.asyncio
async def test_client_login_adds_use_operator_units_header(
    settings_with_client_login: Settings, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.post("https://api-sandbox.direct.yandex.com/json/v5/campaigns").mock(
        return_value=httpx.Response(200, json={"result": {}})
    )

    async with DirectApiClient(settings_with_client_login) as api:
        await api.call("campaigns", "get", {})

    request = route.calls[0].request
    assert request.headers["Client-Login"] == "client-sub-account"
    assert request.headers["Use-Operator-Units"] == "true"


@pytest.mark.asyncio
async def test_last_units_reflects_most_recent_call(
    settings: Settings, respx_mock: respx.MockRouter
) -> None:
    respx_mock.post("https://api-sandbox.direct.yandex.com/json/v5/campaigns").mock(
        side_effect=[
            httpx.Response(200, headers={"Units": "10/23000/24000"}, json={"result": {}}),
            httpx.Response(200, headers={"Units": "15/22985/24000"}, json={"result": {}}),
        ]
    )

    async with DirectApiClient(settings) as api:
        await api.call("campaigns", "get", {})
        assert api.last_units is not None
        assert api.last_units.remaining == 23000

        await api.call("campaigns", "get", {})
        assert api.last_units is not None
        assert api.last_units.remaining == 22985


# --------------------------------------------------------------------------
# Error-code → exception-type mapping.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error_code", "exc_type"),
    [
        (52, AuthError),  # invalid token
        (53, AuthError),  # auth header missing
        (54, AuthError),  # no rights
        (58, AuthError),  # insufficient privileges
        (501, ValidationError),  # input data error
        (503, ValidationError),  # invalid structure
        (8000, ValidationError),  # object not found
        (152, QuotaExceededError),  # daily points
    ],
)
@pytest.mark.asyncio
async def test_error_code_maps_to_typed_exception(
    settings: Settings,
    respx_mock: respx.MockRouter,
    error_code: int,
    exc_type: type[YaDirectError],
) -> None:
    respx_mock.post("https://api-sandbox.direct.yandex.com/json/v5/campaigns").mock(
        return_value=httpx.Response(
            200,
            json={
                "error": {
                    "error_code": error_code,
                    "error_string": "nope",
                    "error_detail": "some detail",
                    "request_id": "req-abc",
                }
            },
        )
    )

    async with DirectApiClient(settings) as api:
        with pytest.raises(exc_type) as exc_info:
            await api.call("campaigns", "get", {})

    # The classified exception carries code + request_id so callers can log it.
    assert exc_info.value.code == error_code
    assert exc_info.value.request_id == "req-abc"


@pytest.mark.asyncio
async def test_unknown_error_code_falls_back_to_base_class(
    settings: Settings, respx_mock: respx.MockRouter
) -> None:
    respx_mock.post("https://api-sandbox.direct.yandex.com/json/v5/campaigns").mock(
        return_value=httpx.Response(
            200,
            json={
                "error": {
                    "error_code": 9999,
                    "error_string": "totally new error",
                }
            },
        )
    )

    async with DirectApiClient(settings) as api:
        # Not in any of the classified sets → the base type.
        with pytest.raises(YaDirectError):
            await api.call("campaigns", "get", {})


# --------------------------------------------------------------------------
# Retry behaviour.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_server_500_is_retried_then_succeeds(
    settings: Settings, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.post("https://api-sandbox.direct.yandex.com/json/v5/campaigns").mock(
        side_effect=[
            httpx.Response(500, text="boom"),
            httpx.Response(502, text="bad gateway"),
            httpx.Response(200, json={"result": {"ok": True}}),
        ]
    )

    async with DirectApiClient(settings) as api:
        result = await api.call("campaigns", "get", {})

    assert result == {"ok": True}
    assert route.call_count == 3


@pytest.mark.asyncio
async def test_timeout_is_retried_then_succeeds(
    settings: Settings, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.post("https://api-sandbox.direct.yandex.com/json/v5/campaigns").mock(
        side_effect=[
            httpx.TimeoutException("timeout"),
            httpx.Response(200, json={"result": {"ok": True}}),
        ]
    )

    async with DirectApiClient(settings) as api:
        result = await api.call("campaigns", "get", {})

    assert result == {"ok": True}
    assert route.call_count == 2


@pytest.mark.asyncio
async def test_transport_error_is_retried(settings: Settings, respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post("https://api-sandbox.direct.yandex.com/json/v5/campaigns").mock(
        side_effect=[
            httpx.ConnectError("conn refused"),
            httpx.Response(200, json={"result": {}}),
        ]
    )

    async with DirectApiClient(settings) as api:
        await api.call("campaigns", "get", {})

    assert route.call_count == 2


@pytest.mark.asyncio
async def test_rate_limit_error_is_retried(
    settings: Settings, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.post("https://api-sandbox.direct.yandex.com/json/v5/campaigns").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "error": {
                        "error_code": 56,
                        "error_string": "too many concurrent",
                    }
                },
            ),
            httpx.Response(200, json={"result": {"ok": True}}),
        ]
    )

    async with DirectApiClient(settings) as api:
        result = await api.call("campaigns", "get", {})

    assert result == {"ok": True}
    assert route.call_count == 2


# wait_random_exponential(max=30) x stop_after_attempt(5) can legitimately
# sleep > 10 s across five tries; raise the per-test cap so retry-exhaustion
# tests don't become a flaky-timeout source. See docs/TESTING.md#coverage.
@pytest.mark.timeout(60)
@pytest.mark.asyncio
async def test_retry_is_exhausted_for_persistent_5xx(
    settings: Settings, respx_mock: respx.MockRouter
) -> None:
    respx_mock.post("https://api-sandbox.direct.yandex.com/json/v5/campaigns").mock(
        return_value=httpx.Response(500, text="boom")
    )

    async with DirectApiClient(settings) as api:
        with pytest.raises(ApiTransientError):
            await api.call("campaigns", "get", {})


@pytest.mark.asyncio
async def test_auth_error_is_not_retried(settings: Settings, respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post("https://api-sandbox.direct.yandex.com/json/v5/campaigns").mock(
        return_value=httpx.Response(
            200,
            json={"error": {"error_code": 52, "error_string": "invalid token"}},
        )
    )

    async with DirectApiClient(settings) as api:
        with pytest.raises(AuthError):
            await api.call("campaigns", "get", {})

    assert route.call_count == 1  # no retries on auth failure


@pytest.mark.asyncio
async def test_validation_error_is_not_retried(
    settings: Settings, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.post("https://api-sandbox.direct.yandex.com/json/v5/campaigns").mock(
        return_value=httpx.Response(
            200,
            json={"error": {"error_code": 501, "error_string": "bad input"}},
        )
    )

    async with DirectApiClient(settings) as api:
        with pytest.raises(ValidationError):
            await api.call("campaigns", "get", {})

    assert route.call_count == 1


# --------------------------------------------------------------------------
# Non-JSON / weird responses.
# --------------------------------------------------------------------------


# Same reasoning as test_retry_is_exhausted_for_persistent_5xx: non-JSON
# body is classified transient and goes through the full retry chain.
@pytest.mark.timeout(60)
@pytest.mark.asyncio
async def test_non_json_body_becomes_transient_error(
    settings: Settings, respx_mock: respx.MockRouter
) -> None:
    respx_mock.post("https://api-sandbox.direct.yandex.com/json/v5/campaigns").mock(
        return_value=httpx.Response(200, text="<html>gateway maintenance</html>")
    )

    async with DirectApiClient(settings) as api:
        with pytest.raises(ApiTransientError):
            await api.call("campaigns", "get", {})


@pytest.mark.asyncio
async def test_result_is_empty_dict_when_response_omits_it(
    settings: Settings, respx_mock: respx.MockRouter
) -> None:
    respx_mock.post("https://api-sandbox.direct.yandex.com/json/v5/campaigns").mock(
        return_value=httpx.Response(200, json={})
    )

    async with DirectApiClient(settings) as api:
        result: dict[str, Any] = await api.call("campaigns", "get", {})

    assert result == {}


# --------------------------------------------------------------------------
# M15.3 follow-up — auto-refresh on AuthError(code=52).
#
# Direct API returns app-level error codes (HTTP 200 + ``error.error_code``
# in body), not HTTP 401. Of the four ``_AUTH_CODES`` (52, 53, 54, 58),
# ONLY code 52 (invalid/expired token) is a legitimate refresh trigger.
# Codes 53 (header missing — our bug), 54 (no rights), 58 (insufficient
# privileges) won't be fixed by a refresh; the tests below pin that
# distinction.
#
# Refresh flow:
# 1. Read refresh_token from keychain (KeyringTokenStore.load).
# 2. Call refresh_access_token to get a new TokenSet.
# 3. Persist the new TokenSet to keychain.
# 4. Mutate Settings tokens + httpx client headers.
# 5. Retry the request once. A second AuthError surfaces as-is.
# --------------------------------------------------------------------------


@pytest.fixture
def memory_keyring(monkeypatch: pytest.MonkeyPatch) -> dict[tuple[str, str], str]:
    """In-memory keyring backend so refresh tests don't touch the OS."""
    import keyring.errors

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


def _seed_keychain_token(
    *,
    refresh_token: str = "1.AQAA-refresh-original",
    access_token: str = "AQAA-access-original",
) -> None:
    """Helper: persist a TokenSet to the in-memory keychain.

    Tests call this to set the operator's "logged in" baseline
    before triggering an AuthError(code=52) on the wire.
    """
    from datetime import UTC, datetime, timedelta

    from yadirect_agent.auth.keychain import KeyringTokenStore
    from yadirect_agent.models.auth import TokenSet

    now = datetime(2026, 4, 30, 12, 0, tzinfo=UTC)
    KeyringTokenStore().save(
        TokenSet(
            access_token=SecretStr(access_token),
            refresh_token=SecretStr(refresh_token),
            token_type="bearer",
            scope=("direct:api", "metrika:read", "metrika:write"),
            obtained_at=now,
            expires_at=now + timedelta(days=365),
        ),
    )


@pytest.mark.asyncio
async def test_auth_error_code_52_triggers_refresh_and_retry_succeeds(
    settings: Settings,
    respx_mock: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
    memory_keyring: dict[tuple[str, str], str],
) -> None:
    # Happy path of the long-idle operator scenario:
    # access_token expired, refresh_token still valid. The client
    # transparently refreshes and the operator never sees an
    # AuthError.
    from datetime import UTC, datetime, timedelta

    from yadirect_agent.models.auth import TokenSet

    _seed_keychain_token(refresh_token="1.AQAA-good-refresh")

    refresh_calls: list[str] = []

    async def fake_refresh(
        *,
        refresh_token: str,
        now: object | None = None,
    ) -> TokenSet:
        refresh_calls.append(refresh_token)
        ts = datetime(2026, 4, 30, 12, 0, tzinfo=UTC)
        return TokenSet(
            access_token=SecretStr("AQAA-access-fresh"),
            refresh_token=SecretStr("1.AQAA-refresh-rotated"),
            token_type="bearer",
            scope=("direct:api", "metrika:read", "metrika:write"),
            obtained_at=ts,
            expires_at=ts + timedelta(days=365),
        )

    monkeypatch.setattr("yadirect_agent.clients.base.refresh_access_token", fake_refresh)

    # First call: 200 + AuthError(code=52). Second call: clean 200.
    route = respx_mock.post("https://api-sandbox.direct.yandex.com/json/v5/campaigns").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "error": {
                        "error_code": 52,
                        "error_string": "invalid_token",
                        "request_id": "req-expired",
                    },
                },
            ),
            httpx.Response(200, json={"result": {"ok": True}}),
        ]
    )

    async with DirectApiClient(settings) as api:
        result = await api.call("campaigns", "get", {})

    # Pin: refresh was called exactly once, with the keychain's
    # refresh_token.
    assert refresh_calls == ["1.AQAA-good-refresh"]
    # Pin: retry succeeded — operator sees the result, not the
    # AuthError.
    assert result == {"ok": True}
    # Pin: the wire saw two POST attempts (original + retry).
    assert route.call_count == 2


@pytest.mark.asyncio
async def test_auth_error_code_52_persists_new_tokenset_to_keychain(
    settings: Settings,
    respx_mock: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
    memory_keyring: dict[tuple[str, str], str],
) -> None:
    # The new TokenSet from the refresh endpoint must land in the
    # keychain so the NEXT process invocation also benefits. A
    # regression that refreshed in-memory only would force a re-
    # login on every cold start after the first expiry.
    from datetime import UTC, datetime, timedelta

    from yadirect_agent.auth.keychain import KeyringTokenStore
    from yadirect_agent.models.auth import TokenSet

    _seed_keychain_token()

    async def fake_refresh(
        *,
        refresh_token: str,
        now: object | None = None,
    ) -> TokenSet:
        ts = datetime(2026, 4, 30, 12, 0, tzinfo=UTC)
        return TokenSet(
            access_token=SecretStr("AQAA-access-NEW"),
            refresh_token=SecretStr("1.AQAA-refresh-NEW"),
            token_type="bearer",
            scope=("direct:api", "metrika:read", "metrika:write"),
            obtained_at=ts,
            expires_at=ts + timedelta(days=365),
        )

    monkeypatch.setattr("yadirect_agent.clients.base.refresh_access_token", fake_refresh)

    respx_mock.post("https://api-sandbox.direct.yandex.com/json/v5/campaigns").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "error": {"error_code": 52, "error_string": "invalid_token"},
                },
            ),
            httpx.Response(200, json={"result": {}}),
        ]
    )

    async with DirectApiClient(settings) as api:
        await api.call("campaigns", "get", {})

    persisted = KeyringTokenStore().load()
    assert persisted is not None
    assert persisted.access_token.get_secret_value() == "AQAA-access-NEW"
    assert persisted.refresh_token.get_secret_value() == "1.AQAA-refresh-NEW"


@pytest.mark.asyncio
async def test_auth_error_code_52_retry_uses_new_authorization_header(
    settings: Settings,
    respx_mock: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
    memory_keyring: dict[tuple[str, str], str],
) -> None:
    # The retry POST must carry the FRESH access token in its
    # Authorization header — otherwise we'd hit the same 52 on the
    # retry. respx captures the request so we can pin the header
    # value the wire saw.
    from datetime import UTC, datetime, timedelta

    from yadirect_agent.models.auth import TokenSet

    _seed_keychain_token()

    async def fake_refresh(
        *,
        refresh_token: str,
        now: object | None = None,
    ) -> TokenSet:
        ts = datetime(2026, 4, 30, 12, 0, tzinfo=UTC)
        return TokenSet(
            access_token=SecretStr("AQAA-access-FRESH-FOR-RETRY"),
            refresh_token=SecretStr("1.AQAA-refresh-still-valid"),
            token_type="bearer",
            scope=("direct:api", "metrika:read", "metrika:write"),
            obtained_at=ts,
            expires_at=ts + timedelta(days=365),
        )

    monkeypatch.setattr("yadirect_agent.clients.base.refresh_access_token", fake_refresh)

    captured_authorizations: list[str] = []

    def capture_then_respond(request: httpx.Request) -> httpx.Response:
        captured_authorizations.append(request.headers.get("authorization", ""))
        if len(captured_authorizations) == 1:
            return httpx.Response(
                200,
                json={"error": {"error_code": 52, "error_string": "invalid_token"}},
            )
        return httpx.Response(200, json={"result": {"ok": True}})

    respx_mock.post("https://api-sandbox.direct.yandex.com/json/v5/campaigns").mock(
        side_effect=capture_then_respond
    )

    async with DirectApiClient(settings) as api:
        await api.call("campaigns", "get", {})

    assert len(captured_authorizations) == 2
    # Pin: original used the Settings-supplied test token; retry
    # used the freshly-refreshed token.
    assert captured_authorizations[0] == "Bearer test-direct-token"
    assert captured_authorizations[1] == "Bearer AQAA-access-FRESH-FOR-RETRY"


@pytest.mark.asyncio
async def test_auth_error_code_52_no_keychain_token_raises_original(
    settings: Settings,
    respx_mock: respx.MockRouter,
    memory_keyring: dict[tuple[str, str], str],
) -> None:
    # Cold-start scenario: the operator's keychain entry was wiped
    # (manual delete, OS reinstall) but the env var still has a
    # stale token. Refresh path can't proceed without a
    # refresh_token; surface the original AuthError so the
    # operator knows to re-run ``auth login``.
    respx_mock.post("https://api-sandbox.direct.yandex.com/json/v5/campaigns").mock(
        return_value=httpx.Response(
            200,
            json={
                "error": {
                    "error_code": 52,
                    "error_string": "invalid_token",
                    "request_id": "req-cold",
                },
            },
        )
    )

    async with DirectApiClient(settings) as api:
        with pytest.raises(AuthError) as exc_info:
            await api.call("campaigns", "get", {})

    # Pin: the original error reaches the caller — operator sees
    # the actionable cause (invalid_token) plus request_id for
    # the audit log.
    assert exc_info.value.code == 52
    assert exc_info.value.request_id == "req-cold"


@pytest.mark.asyncio
async def test_auth_error_code_52_refresh_endpoint_failure_raises_original(
    settings: Settings,
    respx_mock: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
    memory_keyring: dict[tuple[str, str], str],
) -> None:
    # Refresh endpoint itself rejects (refresh_token expired,
    # operator revoked the grant at yandex.ru/profile/access). The
    # original AuthError must surface — the inner refresh failure
    # is logged but doesn't replace the user-visible cause.
    _seed_keychain_token()

    async def failing_refresh(
        *,
        refresh_token: str,
        now: object | None = None,
    ) -> TokenSet:
        from yadirect_agent.exceptions import AuthError as _AuthError

        raise _AuthError("refresh_token revoked")

    monkeypatch.setattr("yadirect_agent.clients.base.refresh_access_token", failing_refresh)

    respx_mock.post("https://api-sandbox.direct.yandex.com/json/v5/campaigns").mock(
        return_value=httpx.Response(
            200,
            json={"error": {"error_code": 52, "error_string": "invalid_token"}},
        )
    )

    async with DirectApiClient(settings) as api:
        with pytest.raises(AuthError) as exc_info:
            await api.call("campaigns", "get", {})

    # The original wire error wins; the inner refresh failure was
    # logged but does NOT replace the user-visible cause.
    assert exc_info.value.code == 52


@pytest.mark.asyncio
async def test_auth_error_code_52_retry_failure_does_not_loop(
    settings: Settings,
    respx_mock: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
    memory_keyring: dict[tuple[str, str], str],
) -> None:
    # Refresh succeeds, retry POST hits another 52 (e.g. the
    # operator's grant was revoked between refresh and retry).
    # MUST NOT trigger a second refresh — the retry boundary is
    # exactly one. An infinite loop would deadlock the agent.
    from datetime import UTC, datetime, timedelta

    from yadirect_agent.models.auth import TokenSet

    _seed_keychain_token()

    refresh_call_count = 0

    async def counted_refresh(
        *,
        refresh_token: str,
        now: object | None = None,
    ) -> TokenSet:
        nonlocal refresh_call_count
        refresh_call_count += 1
        ts = datetime(2026, 4, 30, 12, 0, tzinfo=UTC)
        return TokenSet(
            access_token=SecretStr("AQAA-fresh"),
            refresh_token=SecretStr("1.AQAA-refresh"),
            token_type="bearer",
            scope=("direct:api", "metrika:read", "metrika:write"),
            obtained_at=ts,
            expires_at=ts + timedelta(days=365),
        )

    monkeypatch.setattr("yadirect_agent.clients.base.refresh_access_token", counted_refresh)

    route = respx_mock.post("https://api-sandbox.direct.yandex.com/json/v5/campaigns").mock(
        return_value=httpx.Response(
            200,
            json={"error": {"error_code": 52, "error_string": "still_invalid"}},
        )
    )

    async with DirectApiClient(settings) as api:
        with pytest.raises(AuthError) as exc_info:
            await api.call("campaigns", "get", {})

    assert exc_info.value.code == 52
    # Exactly one refresh — never two.
    assert refresh_call_count == 1
    # Original + retry only — no third attempt.
    assert route.call_count == 2


@pytest.mark.parametrize("non_refreshable_code", [53, 54, 58])
@pytest.mark.asyncio
async def test_other_auth_codes_do_not_trigger_refresh(
    settings: Settings,
    respx_mock: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
    memory_keyring: dict[tuple[str, str], str],
    non_refreshable_code: int,
) -> None:
    # Codes 53 / 54 / 58 are auth failures that a refresh CANNOT
    # fix (header missing — our bug; no rights — operator's grant
    # wasn't issued for this scope; insufficient privileges —
    # operator's account doesn't own the campaign). Refreshing
    # would burn an API call for nothing and mask the real cause.
    _seed_keychain_token()

    refresh_called = False

    async def spy_refresh(
        *,
        refresh_token: str,
        now: object | None = None,
    ) -> TokenSet:
        nonlocal refresh_called
        refresh_called = True
        msg = "should not be called"
        raise AssertionError(msg)

    monkeypatch.setattr("yadirect_agent.clients.base.refresh_access_token", spy_refresh)

    respx_mock.post("https://api-sandbox.direct.yandex.com/json/v5/campaigns").mock(
        return_value=httpx.Response(
            200,
            json={
                "error": {
                    "error_code": non_refreshable_code,
                    "error_string": "unrelated auth issue",
                },
            },
        )
    )

    async with DirectApiClient(settings) as api:
        with pytest.raises(AuthError) as exc_info:
            await api.call("campaigns", "get", {})

    assert exc_info.value.code == non_refreshable_code
    assert refresh_called is False
