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
_COUNTERS_URL = "https://api-metrika.yandex.net/management/v1/counters"


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

    @respx.mock
    async def test_huge_error_body_capped_in_message(
        self,
        settings: Settings,
    ) -> None:
        # A misbehaving proxy can return a multi-megabyte HTML error
        # page. Without capping, that whole body ends up as the
        # exception's str(), and from there bloats every audit JSONL
        # row that records the failure. (auditor M6 MEDIUM-3.)
        huge_body = "X" * 100_000
        respx.get(_GOALS_URL).mock(
            return_value=httpx.Response(400, text=huge_body),
        )

        async with MetrikaService(settings) as svc:
            with pytest.raises(ValidationError) as exc_info:
                await svc.get_goals(counter_id=12345)

        # Message must be bounded — exact cap is implementation detail
        # but it must be far smaller than the raw 100k body.
        assert len(str(exc_info.value)) < 2000

    @respx.mock
    async def test_huge_structured_message_capped(
        self,
        settings: Settings,
    ) -> None:
        # Same cap applies when the message comes from the structured
        # ``errors[0].message`` field — a malicious server could stuff
        # arbitrary length there too.
        huge_message = "X" * 100_000
        respx.get(_GOALS_URL).mock(
            return_value=httpx.Response(
                400,
                json={"errors": [{"message": huge_message}]},
            ),
        )

        async with MetrikaService(settings) as svc:
            with pytest.raises(ValidationError) as exc_info:
                await svc.get_goals(counter_id=12345)

        assert len(str(exc_info.value)) < 2000


class TestGetCounters:
    @respx.mock
    async def test_happy_path_returns_typed_counters(
        self,
        settings: Settings,
    ) -> None:
        respx.get(_COUNTERS_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "counters": [
                        {
                            "id": 12345,
                            "name": "my-shop",
                            "site": "example.com",
                            "status": "Active",
                        },
                        {"id": 67890, "name": "secondary"},
                    ],
                },
            ),
        )

        async with MetrikaService(settings) as svc:
            counters = await svc.get_counters()

        assert len(counters) == 2
        assert counters[0].id == 12345
        assert counters[0].name == "my-shop"
        assert counters[0].site == "example.com"
        assert counters[1].id == 67890
        assert counters[1].site is None  # field optional

    @respx.mock
    async def test_empty_account_returns_empty_list(
        self,
        settings: Settings,
    ) -> None:
        respx.get(_COUNTERS_URL).mock(
            return_value=httpx.Response(200, json={"counters": []}),
        )

        async with MetrikaService(settings) as svc:
            counters = await svc.get_counters()

        assert counters == []

    @respx.mock
    async def test_401_raises_auth_error(self, settings: Settings) -> None:
        respx.get(_COUNTERS_URL).mock(
            return_value=httpx.Response(
                401,
                json={"errors": [{"message": "token invalid"}]},
            ),
        )

        async with MetrikaService(settings) as svc:
            with pytest.raises(AuthError):
                await svc.get_counters()


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


# --------------------------------------------------------------------------
# M15.3 follow-up — auto-refresh on Metrika HTTP 401.
#
# Parity with the Direct surface (#67): a long-idle operator hits a stale
# access token, the client transparently refreshes via the keychain
# TokenSet and retries once. Operator never sees AuthError.
#
# Differences vs Direct: catch boundary is the raw HTTP 401 (not an
# app-level error code), so the retry happens inside ``_request`` after
# the tenacity envelope completes but BEFORE the caller raises
# ``AuthError`` via ``_classify_terminal``.
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
    access_token: str = "AQAA-metrika-original",
) -> None:
    """Helper: persist a TokenSet to the in-memory keychain."""
    from datetime import UTC, datetime, timedelta

    from pydantic import SecretStr

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


def _fake_refresh_factory(
    *,
    new_access: str = "AQAA-metrika-FRESH",
    new_refresh: str = "1.AQAA-rotated",
    refresh_calls: list[str] | None = None,
):
    """Build a fake refresh that returns a TokenSet with the given values.

    Optionally records the inbound refresh_token for assertion.
    """
    from datetime import UTC, datetime, timedelta

    from pydantic import SecretStr

    from yadirect_agent.models.auth import TokenSet

    async def fake_refresh(
        *,
        refresh_token: str,
        now: object | None = None,
    ) -> TokenSet:
        if refresh_calls is not None:
            refresh_calls.append(refresh_token)
        ts = datetime(2026, 4, 30, 12, 0, tzinfo=UTC)
        return TokenSet(
            access_token=SecretStr(new_access),
            refresh_token=SecretStr(new_refresh),
            token_type="bearer",
            scope=("direct:api", "metrika:read", "metrika:write"),
            obtained_at=ts,
            expires_at=ts + timedelta(days=365),
        )

    return fake_refresh


class TestAutoRefreshOn401:
    @respx.mock
    async def test_401_triggers_refresh_and_retry_succeeds(
        self,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
        memory_keyring: dict[tuple[str, str], str],
    ) -> None:
        # Long-idle operator: access token expired, refresh_token
        # still valid. Metrika returns 401 → client refreshes
        # transparently → retry returns 200 with the goals payload
        # the caller asked for.
        _seed_keychain_token(refresh_token="1.AQAA-good-refresh")

        refresh_calls: list[str] = []
        monkeypatch.setattr(
            "yadirect_agent.clients.metrika.refresh_access_token",
            _fake_refresh_factory(refresh_calls=refresh_calls),
        )

        respx.get(_GOALS_URL).mock(
            side_effect=[
                httpx.Response(
                    401,
                    json={"errors": [{"message": "token invalid"}]},
                ),
                httpx.Response(
                    200,
                    json={"goals": [{"id": 100, "name": "Order completed", "type": "number"}]},
                ),
            ],
        )

        async with MetrikaService(settings) as svc:
            goals = await svc.get_goals(counter_id=12345)

        # Pin: refresh fired exactly once with the keychain's
        # refresh_token (not the original env-var access token).
        assert refresh_calls == ["1.AQAA-good-refresh"]
        # Pin: caller saw the retry's payload, not the AuthError.
        assert len(goals) == 1
        assert goals[0].id == 100

    @respx.mock
    async def test_401_persists_new_tokenset_to_keychain(
        self,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
        memory_keyring: dict[tuple[str, str], str],
    ) -> None:
        # Same persistence contract as Direct: the refreshed
        # TokenSet must land in the keychain so the NEXT process
        # invocation also benefits. A regression that refreshed
        # in-memory only would force re-login on every cold start.
        from yadirect_agent.auth.keychain import KeyringTokenStore

        _seed_keychain_token()

        monkeypatch.setattr(
            "yadirect_agent.clients.metrika.refresh_access_token",
            _fake_refresh_factory(new_access="AQAA-metrika-PERSISTED", new_refresh="1.AQAA-rot"),
        )

        respx.get(_GOALS_URL).mock(
            side_effect=[
                httpx.Response(401, json={"errors": [{"message": "expired"}]}),
                httpx.Response(200, json={"goals": []}),
            ],
        )

        async with MetrikaService(settings) as svc:
            await svc.get_goals(counter_id=12345)

        persisted = KeyringTokenStore().load()
        assert persisted is not None
        assert persisted.access_token.get_secret_value() == "AQAA-metrika-PERSISTED"
        assert persisted.refresh_token.get_secret_value() == "1.AQAA-rot"

    @respx.mock
    async def test_401_retry_uses_fresh_oauth_header(
        self,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
        memory_keyring: dict[tuple[str, str], str],
    ) -> None:
        # The retry GET must carry the FRESH access token in its
        # Authorization header — otherwise we'd hit the same 401
        # on retry. Metrika uses ``OAuth <token>`` (NOT
        # ``Bearer``), so the assertion pins both the scheme AND
        # the value.
        _seed_keychain_token()

        monkeypatch.setattr(
            "yadirect_agent.clients.metrika.refresh_access_token",
            _fake_refresh_factory(new_access="AQAA-FRESH-FOR-RETRY"),
        )

        captured_authorizations: list[str] = []

        def capture_then_respond(request: httpx.Request) -> httpx.Response:
            captured_authorizations.append(request.headers.get("authorization", ""))
            if len(captured_authorizations) == 1:
                return httpx.Response(401, json={"errors": [{"message": "expired"}]})
            return httpx.Response(200, json={"goals": []})

        respx.get(_GOALS_URL).mock(side_effect=capture_then_respond)

        async with MetrikaService(settings) as svc:
            await svc.get_goals(counter_id=12345)

        assert len(captured_authorizations) == 2
        # Original used the Settings-supplied test token; retry
        # uses the freshly-refreshed token under the Metrika
        # ``OAuth`` scheme.
        assert captured_authorizations[0] == "OAuth test-metrika-token"
        assert captured_authorizations[1] == "OAuth AQAA-FRESH-FOR-RETRY"

    @respx.mock
    async def test_401_no_keychain_token_raises_original_auth_error(
        self,
        settings: Settings,
        memory_keyring: dict[tuple[str, str], str],
    ) -> None:
        # Cold-start: keychain entry was wiped (manual delete, OS
        # reinstall) but env var still has a stale token. Refresh
        # path can't proceed without a refresh_token; surface the
        # original AuthError so the operator knows to re-run
        # ``auth login``.
        respx.get(_GOALS_URL).mock(
            return_value=httpx.Response(
                401,
                json={"errors": [{"message": "token invalid"}]},
            ),
        )

        async with MetrikaService(settings) as svc:
            with pytest.raises(AuthError, match="token invalid"):
                await svc.get_goals(counter_id=12345)

    @respx.mock
    async def test_401_refresh_endpoint_failure_raises_original(
        self,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
        memory_keyring: dict[tuple[str, str], str],
    ) -> None:
        # Refresh endpoint rejects (refresh_token revoked at
        # yandex.ru/profile/access). Original wire AuthError must
        # surface; inner refresh failure is logged but doesn't
        # replace the user-visible cause.
        _seed_keychain_token()

        async def failing_refresh(
            *,
            refresh_token: str,
            now: object | None = None,
        ) -> object:
            raise AuthError("refresh_token revoked")

        monkeypatch.setattr("yadirect_agent.clients.metrika.refresh_access_token", failing_refresh)

        respx.get(_GOALS_URL).mock(
            return_value=httpx.Response(
                401,
                json={"errors": [{"message": "token invalid"}]},
            ),
        )

        async with MetrikaService(settings) as svc:
            with pytest.raises(AuthError, match="token invalid"):
                await svc.get_goals(counter_id=12345)

    @respx.mock
    async def test_401_retry_failure_does_not_loop(
        self,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
        memory_keyring: dict[tuple[str, str], str],
    ) -> None:
        # Refresh succeeds, retry GET hits another 401 (e.g. the
        # operator's grant was revoked between refresh and retry).
        # MUST NOT trigger a second refresh — exactly one retry.
        _seed_keychain_token()

        refresh_call_count = 0

        async def counted_refresh(
            *,
            refresh_token: str,
            now: object | None = None,
        ) -> object:
            nonlocal refresh_call_count
            refresh_call_count += 1
            from datetime import UTC, datetime, timedelta

            from pydantic import SecretStr

            from yadirect_agent.models.auth import TokenSet

            ts = datetime(2026, 4, 30, 12, 0, tzinfo=UTC)
            return TokenSet(
                access_token=SecretStr("AQAA-fresh"),
                refresh_token=SecretStr("1.AQAA-refresh"),
                token_type="bearer",
                scope=("direct:api", "metrika:read", "metrika:write"),
                obtained_at=ts,
                expires_at=ts + timedelta(days=365),
            )

        monkeypatch.setattr("yadirect_agent.clients.metrika.refresh_access_token", counted_refresh)

        route = respx.get(_GOALS_URL).mock(
            return_value=httpx.Response(
                401,
                json={"errors": [{"message": "still invalid"}]},
            ),
        )

        async with MetrikaService(settings) as svc:
            with pytest.raises(AuthError):
                await svc.get_goals(counter_id=12345)

        # Exactly one refresh — never two.
        assert refresh_call_count == 1
        # Original + retry only (each goes through 4 tenacity
        # attempts but 401 is NOT a retryable status, so each
        # _request call hits the wire exactly once).
        assert route.call_count == 2

    @respx.mock
    async def test_403_does_not_trigger_refresh(
        self,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
        memory_keyring: dict[tuple[str, str], str],
    ) -> None:
        # 403 Forbidden means the token is valid but lacks the
        # scope (operator never granted ``metrika:read``, or
        # revoked it). Refresh would burn an OAuth call for
        # nothing — token-self-grant is not auto-elevatable. Pin
        # via spy that refresh was NOT called.
        _seed_keychain_token()

        refresh_called = False

        async def spy_refresh(
            *,
            refresh_token: str,
            now: object | None = None,
        ) -> object:
            nonlocal refresh_called
            refresh_called = True
            msg = "should not be called"
            raise AssertionError(msg)

        monkeypatch.setattr("yadirect_agent.clients.metrika.refresh_access_token", spy_refresh)

        respx.get(_GOALS_URL).mock(
            return_value=httpx.Response(
                403,
                json={"errors": [{"message": "scope insufficient"}]},
            ),
        )

        async with MetrikaService(settings) as svc:
            with pytest.raises(AuthError, match="scope insufficient"):
                await svc.get_goals(counter_id=12345)

        assert refresh_called is False

    @respx.mock
    async def test_404_does_not_trigger_refresh(
        self,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
        memory_keyring: dict[tuple[str, str], str],
    ) -> None:
        # Non-auth 4xx (e.g. 404 Not Found from a wrong counter
        # ID) is not an auth issue; refresh is irrelevant.
        _seed_keychain_token()

        refresh_called = False

        async def spy_refresh(
            *,
            refresh_token: str,
            now: object | None = None,
        ) -> object:
            nonlocal refresh_called
            refresh_called = True
            msg = "should not be called"
            raise AssertionError(msg)

        monkeypatch.setattr("yadirect_agent.clients.metrika.refresh_access_token", spy_refresh)

        respx.get(_GOALS_URL).mock(
            return_value=httpx.Response(
                404,
                json={"errors": [{"message": "counter not found"}]},
            ),
        )

        async with MetrikaService(settings) as svc:
            with pytest.raises(ValidationError, match="counter not found"):
                await svc.get_goals(counter_id=12345)

        assert refresh_called is False
