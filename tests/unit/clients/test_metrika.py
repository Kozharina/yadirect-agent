"""Tests for ``MetrikaService`` HTTP client (M6 basic).

Covered:

- ``get_goals``: happy path, 401→AuthError, 429→retry-then-success,
  500→retry-exhausted, transport timeout→retry,
  empty-counter (no goals) → empty list.

Pattern follows ``tests/unit/clients/test_base.py`` and
``test_direct.py``: respx mocks the HTTP boundary, the service
runs against a fake ``Settings`` from conftest.

Why we test transport-level concerns here and decision-level in
the service tests: the client only knows "how to talk to Metrika";
the service decides "what to do with goals once we have them".
"""

from __future__ import annotations

import httpx
import pytest
import respx

from yadirect_agent.clients.metrika import MetrikaService
from yadirect_agent.config import Settings
from yadirect_agent.exceptions import ApiTransientError, AuthError, ValidationError

_GOALS_URL = "https://api-metrika.yandex.net/management/v1/counter/12345/goals"


class TestGetGoals:
    @respx.mock
    async def test_happy_path_returns_typed_goals(self, settings: Settings) -> None:
        respx.get(_GOALS_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "goals": [
                        {"id": 100, "name": "Order completed", "type": "number"},
                        {"id": 101, "name": "Cart added", "type": "action"},
                    ],
                },
            ),
        )

        async with MetrikaService(settings) as svc:
            goals = await svc.get_goals(counter_id=12345)

        assert len(goals) == 2
        assert goals[0].id == 100
        assert goals[0].name == "Order completed"
        assert goals[0].type == "number"
        assert goals[1].type == "action"

    @respx.mock
    async def test_empty_counter_returns_empty_list(self, settings: Settings) -> None:
        respx.get(_GOALS_URL).mock(return_value=httpx.Response(200, json={"goals": []}))

        async with MetrikaService(settings) as svc:
            goals = await svc.get_goals(counter_id=12345)

        assert goals == []

    @respx.mock
    async def test_401_raises_auth_error(self, settings: Settings) -> None:
        respx.get(_GOALS_URL).mock(
            return_value=httpx.Response(
                401,
                json={"errors": [{"error_type": "unauthorized", "message": "token invalid"}]},
            ),
        )

        async with MetrikaService(settings) as svc:
            with pytest.raises(AuthError, match="token invalid"):
                await svc.get_goals(counter_id=12345)

    @respx.mock
    async def test_429_retries_then_succeeds(self, settings: Settings) -> None:
        # First two calls 429, third succeeds — retry budget is 4 attempts.
        respx.get(_GOALS_URL).mock(
            side_effect=[
                httpx.Response(429, json={"errors": [{"message": "slow down"}]}),
                httpx.Response(429, json={"errors": [{"message": "slow down"}]}),
                httpx.Response(200, json={"goals": [{"id": 1, "name": "x", "type": "number"}]}),
            ],
        )

        async with MetrikaService(settings) as svc:
            goals = await svc.get_goals(counter_id=12345)

        assert len(goals) == 1
        assert goals[0].id == 1

    @respx.mock
    async def test_500_eventually_raises_transient(self, settings: Settings) -> None:
        respx.get(_GOALS_URL).mock(
            return_value=httpx.Response(
                500,
                json={"errors": [{"message": "server unavailable"}]},
            ),
        )

        async with MetrikaService(settings) as svc:
            with pytest.raises(ApiTransientError):
                await svc.get_goals(counter_id=12345)

    @respx.mock
    async def test_timeout_retries_then_succeeds(self, settings: Settings) -> None:
        respx.get(_GOALS_URL).mock(
            side_effect=[
                httpx.TimeoutException("read timed out"),
                httpx.Response(200, json={"goals": []}),
            ],
        )

        async with MetrikaService(settings) as svc:
            goals = await svc.get_goals(counter_id=12345)

        assert goals == []

    @respx.mock
    async def test_authorization_header_uses_metrika_token(self, settings: Settings) -> None:
        route = respx.get(_GOALS_URL).mock(
            return_value=httpx.Response(200, json={"goals": []}),
        )

        async with MetrikaService(settings) as svc:
            await svc.get_goals(counter_id=12345)

        assert route.called
        call = route.calls.last
        assert call.request.headers["authorization"] == "OAuth test-metrika-token"

    @respx.mock
    async def test_400_raises_validation_error(self, settings: Settings) -> None:
        respx.get(_GOALS_URL).mock(
            return_value=httpx.Response(
                400,
                json={"errors": [{"message": "counter_id must be positive"}]},
            ),
        )

        async with MetrikaService(settings) as svc:
            with pytest.raises(ValidationError, match="counter_id"):
                await svc.get_goals(counter_id=12345)
