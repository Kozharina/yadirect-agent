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

from yadirect_agent.clients.base import DirectApiClient, UnitsInfo
from yadirect_agent.config import Settings
from yadirect_agent.exceptions import (
    ApiTransientError,
    AuthError,
    QuotaExceededError,
    ValidationError,
    YaDirectError,
)

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
