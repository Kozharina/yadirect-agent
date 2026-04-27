"""Tests for ``MetrikaService`` HTTP client (M6 basic).

Covered:

- ``get_goals``: happy path, 401→AuthError, 429→retry-then-success,
  500→retry-exhausted, transport timeout→retry,
  empty-counter (no goals) → empty list.
- ``get_report``: query-string serialisation of metrics/dimensions/
  date1/date2/filters, parsing of ``data: [...]`` envelope into
  ``ReportRow`` instances, empty result, validation error on
  unknown metric.
- ``get_conversion_by_source``: composes the right filter and
  parses sources to integer counts.

Pattern follows ``tests/unit/clients/test_base.py`` and
``test_direct.py``: respx mocks the HTTP boundary, the service
runs against a fake ``Settings`` from conftest.

Why we test transport-level concerns here and decision-level in
the service tests: the client only knows "how to talk to Metrika";
the service decides "what to do with goals once we have them".
"""

from __future__ import annotations

from datetime import date

import httpx
import pytest
import respx

from yadirect_agent.clients.metrika import MetrikaService
from yadirect_agent.config import Settings
from yadirect_agent.exceptions import (
    ApiTransientError,
    AuthError,
    RateLimitError,
    ValidationError,
)
from yadirect_agent.models.metrika import DateRange

_GOALS_URL = "https://api-metrika.yandex.net/management/v1/counter/12345/goals"
_REPORT_URL = "https://api-metrika.yandex.net/stat/v1/data"


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
    async def test_timeout_exhausted_raises_transient_error(
        self,
        settings: Settings,
    ) -> None:
        # All four attempts time out — must surface as ApiTransientError,
        # not a raw httpx.TimeoutException. Caller error handlers in the
        # agent loop catch our typed exceptions only; a leaked transport
        # error would be classified as a generic tool failure and lose
        # the retry signal. (security-auditor M6 HIGH-1.)
        respx.get(_GOALS_URL).mock(side_effect=httpx.TimeoutException("read timed out"))

        async with MetrikaService(settings) as svc:
            with pytest.raises(ApiTransientError, match="network"):
                await svc.get_goals(counter_id=12345)

    @respx.mock
    async def test_429_exhausted_raises_rate_limit_error(
        self,
        settings: Settings,
    ) -> None:
        # Distinct from 5xx-exhaustion: 429 means "back off", not "server
        # is sick". The agent loop must see RateLimitError, not
        # ApiTransientError, to make the right decision. (security-auditor
        # M6 MEDIUM-5.)
        respx.get(_GOALS_URL).mock(
            return_value=httpx.Response(
                429,
                json={"errors": [{"message": "rate limit exceeded"}]},
            ),
        )

        async with MetrikaService(settings) as svc:
            with pytest.raises(RateLimitError):
                await svc.get_goals(counter_id=12345)

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


class TestGetReport:
    @respx.mock
    async def test_happy_path_parses_rows(self, settings: Settings) -> None:
        respx.get(_REPORT_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "dimensions": [{"name": "Yandex Direct"}],
                            "metrics": [120.0, 850.5, 5.0],
                        },
                        {
                            "dimensions": [{"name": "Yandex Search"}],
                            "metrics": [42.0, 0.0, 1.0],
                        },
                    ],
                },
            ),
        )

        async with MetrikaService(settings) as svc:
            rows = await svc.get_report(
                counter_id=12345,
                metrics=["ym:s:visits", "ym:ad:directCost", "ym:s:goal100conversions"],
                dimensions=["ym:s:lastDirectClickSourceName"],
                date_range=DateRange(start=date(2026, 4, 1), end=date(2026, 4, 7)),
            )

        assert len(rows) == 2
        assert rows[0].dimensions[0]["name"] == "Yandex Direct"
        assert rows[0].metrics == [120.0, 850.5, 5.0]
        assert rows[1].metrics == [42.0, 0.0, 1.0]

    @respx.mock
    async def test_query_string_serialisation(self, settings: Settings) -> None:
        # Metrika expects comma-separated metric/dimension lists, ISO dates,
        # and the counter id as ``ids``. Test wire format directly so a
        # silent rename ('ids' → 'counter_id') doesn't go unnoticed.
        route = respx.get(_REPORT_URL).mock(
            return_value=httpx.Response(200, json={"data": []}),
        )

        async with MetrikaService(settings) as svc:
            await svc.get_report(
                counter_id=12345,
                metrics=["ym:s:visits", "ym:ad:directCost"],
                dimensions=["ym:s:lastDirectClickSourceName"],
                date_range=DateRange(start=date(2026, 4, 1), end=date(2026, 4, 7)),
            )

        assert route.called
        params = dict(route.calls.last.request.url.params)
        assert params["ids"] == "12345"
        assert params["metrics"] == "ym:s:visits,ym:ad:directCost"
        assert params["dimensions"] == "ym:s:lastDirectClickSourceName"
        assert params["date1"] == "2026-04-01"
        assert params["date2"] == "2026-04-07"

    @respx.mock
    async def test_empty_result(self, settings: Settings) -> None:
        respx.get(_REPORT_URL).mock(return_value=httpx.Response(200, json={"data": []}))

        async with MetrikaService(settings) as svc:
            rows = await svc.get_report(
                counter_id=12345,
                metrics=["ym:s:visits"],
                dimensions=[],
                date_range=DateRange(start=date(2026, 4, 1), end=date(2026, 4, 7)),
            )

        assert rows == []

    @respx.mock
    async def test_optional_filter_passed_through(self, settings: Settings) -> None:
        route = respx.get(_REPORT_URL).mock(
            return_value=httpx.Response(200, json={"data": []}),
        )

        async with MetrikaService(settings) as svc:
            await svc.get_report(
                counter_id=12345,
                metrics=["ym:s:visits"],
                dimensions=[],
                date_range=DateRange(start=date(2026, 4, 1), end=date(2026, 4, 7)),
                filters="ym:s:lastDirectClickSourceName=='Yandex Direct'",
            )

        params = dict(route.calls.last.request.url.params)
        assert params["filters"] == "ym:s:lastDirectClickSourceName=='Yandex Direct'"

    @respx.mock
    async def test_filter_omitted_when_none(self, settings: Settings) -> None:
        # Don't send ``filters=`` (empty) — Metrika rejects empty filter.
        route = respx.get(_REPORT_URL).mock(
            return_value=httpx.Response(200, json={"data": []}),
        )

        async with MetrikaService(settings) as svc:
            await svc.get_report(
                counter_id=12345,
                metrics=["ym:s:visits"],
                dimensions=[],
                date_range=DateRange(start=date(2026, 4, 1), end=date(2026, 4, 7)),
            )

        params = dict(route.calls.last.request.url.params)
        assert "filters" not in params

    @respx.mock
    async def test_400_unknown_metric_raises_validation_error(
        self,
        settings: Settings,
    ) -> None:
        respx.get(_REPORT_URL).mock(
            return_value=httpx.Response(
                400,
                json={"errors": [{"message": "Unknown metric: ym:s:notAThing"}]},
            ),
        )

        async with MetrikaService(settings) as svc:
            with pytest.raises(ValidationError, match="Unknown metric"):
                await svc.get_report(
                    counter_id=12345,
                    metrics=["ym:s:notAThing"],
                    dimensions=[],
                    date_range=DateRange(start=date(2026, 4, 1), end=date(2026, 4, 7)),
                )

    @respx.mock
    async def test_500_eventually_raises_transient(self, settings: Settings) -> None:
        respx.get(_REPORT_URL).mock(return_value=httpx.Response(503))

        async with MetrikaService(settings) as svc:
            with pytest.raises(ApiTransientError):
                await svc.get_report(
                    counter_id=12345,
                    metrics=["ym:s:visits"],
                    dimensions=[],
                    date_range=DateRange(start=date(2026, 4, 1), end=date(2026, 4, 7)),
                )


class TestGetConversionBySource:
    @respx.mock
    async def test_returns_source_to_count_mapping(self, settings: Settings) -> None:
        respx.get(_REPORT_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "dimensions": [{"name": "Yandex Direct"}],
                            "metrics": [12.0],
                        },
                        {
                            "dimensions": [{"name": "Organic Search"}],
                            "metrics": [5.0],
                        },
                        {
                            "dimensions": [{"name": "Direct Traffic"}],
                            "metrics": [3.0],
                        },
                    ],
                },
            ),
        )

        async with MetrikaService(settings) as svc:
            conv = await svc.get_conversion_by_source(
                counter_id=12345,
                goal_id=100,
                date_range=DateRange(start=date(2026, 4, 1), end=date(2026, 4, 7)),
            )

        assert conv == {
            "Yandex Direct": 12,
            "Organic Search": 5,
            "Direct Traffic": 3,
        }

    @respx.mock
    async def test_uses_correct_metric_for_goal(self, settings: Settings) -> None:
        # The composed metric must reference the specific goal_id —
        # "ym:s:goal<id>conversions". Without this the agent gets
        # total conversions, not goal-specific ones, and budget
        # decisions silently optimize for the wrong target.
        route = respx.get(_REPORT_URL).mock(
            return_value=httpx.Response(200, json={"data": []}),
        )

        async with MetrikaService(settings) as svc:
            await svc.get_conversion_by_source(
                counter_id=12345,
                goal_id=42,
                date_range=DateRange(start=date(2026, 4, 1), end=date(2026, 4, 7)),
            )

        params = dict(route.calls.last.request.url.params)
        assert "ym:s:goal42conversions" in params["metrics"]
        assert "ym:s:lastDirectClickSourceName" in params["dimensions"]

    @respx.mock
    async def test_empty_result_returns_empty_dict(self, settings: Settings) -> None:
        respx.get(_REPORT_URL).mock(return_value=httpx.Response(200, json={"data": []}))

        async with MetrikaService(settings) as svc:
            conv = await svc.get_conversion_by_source(
                counter_id=12345,
                goal_id=100,
                date_range=DateRange(start=date(2026, 4, 1), end=date(2026, 4, 7)),
            )

        assert conv == {}
