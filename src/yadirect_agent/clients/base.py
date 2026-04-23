"""Base HTTP client for Yandex Direct API v5.

This is the most important file in the project. Everything the agent does
goes through here, so the guarantees we make here (retries, error types,
logging, rate limiting) are the guarantees the whole system has.

Yandex Direct API v5 specifics baked in:
- JSON-RPC-ish body: {"method": "...", "params": {...}}
- One URL per service: /json/v5/{service}  (campaigns, ads, keywords, ...)
- Auth: Bearer token
- Accept-Language: ru  (affects error messages, not data)
- Client-Login header for agency accounts
- "Units" response header format: "spent/available/daily_limit"
- Error envelope: {"error": {"error_code", "error_string", "error_detail",
                             "request_id"}}
- HTTP 200 on logical errors too — always check the body

Reports service is different (async, TSV output) — lives in clients/direct.py
near the reports method, not here.
"""

from __future__ import annotations

from typing import Any, Self

import httpx
import structlog
from tenacity import (
    AsyncRetrying,
    RetryError,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
    wait_random_exponential,
)

from ..config import Settings
from ..exceptions import (
    ApiTransientError,
    AuthError,
    QuotaExceededError,
    RateLimitError,
    ValidationError,
    YaDirectError,
)

# Error codes treated as "don't retry, input is wrong".
# See https://yandex.ru/dev/direct/doc/dg/concepts/errors.html for the full list.
_VALIDATION_CODES: frozenset[int] = frozenset(
    {
        53,  # Auth header missing (our bug)
        54,  # No rights for operation
        58,  # Insufficient privileges
        501,  # Input data error
        503,  # Invalid structure
        506,  # Invalid parameter value
        8000,  # Object not found
    }
)
_AUTH_CODES: frozenset[int] = frozenset({52, 53, 54, 58})
_QUOTA_CODES: frozenset[int] = frozenset({152, 506})  # daily points / etc.
_RATE_LIMIT_CODES: frozenset[int] = frozenset({56, 506})  # too many concurrent


class UnitsInfo:
    """Parsed 'Units' response header.

    Example header value: "10/23750/24000" =>
      last_cost=10, remaining=23750, daily_limit=24000
    """

    __slots__ = ("daily_limit", "last_cost", "remaining")

    def __init__(self, last_cost: int, remaining: int, daily_limit: int) -> None:
        self.last_cost = last_cost
        self.remaining = remaining
        self.daily_limit = daily_limit

    @classmethod
    def parse(cls, value: str | None) -> Self | None:
        if not value:
            return None
        try:
            last, rem, limit = (int(p) for p in value.split("/"))
        except (ValueError, AttributeError):
            return None
        return cls(last, rem, limit)

    @property
    def pct_used(self) -> float:
        return 1.0 - (self.remaining / self.daily_limit) if self.daily_limit else 0.0


class DirectApiClient:
    """Async client for Yandex Direct API v5.

    Use as an async context manager:

        async with DirectApiClient(settings) as api:
            result = await api.call("campaigns", "get", {"SelectionCriteria": {}})
    """

    def __init__(
        self,
        settings: Settings,
        *,
        timeout: float = 30.0,
    ) -> None:
        self._settings = settings
        self._logger = structlog.get_logger().bind(component="direct_client")
        self._client = httpx.AsyncClient(
            base_url=settings.direct_base_url,
            timeout=timeout,
            headers=self._build_default_headers(),
        )
        self._last_units: UnitsInfo | None = None

    def _build_default_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {
            "Authorization": f"Bearer {self._settings.yandex_direct_token.get_secret_value()}",
            "Accept-Language": "ru",
            "Content-Type": "application/json; charset=utf-8",
        }
        if self._settings.yandex_client_login:
            headers["Client-Login"] = self._settings.yandex_client_login
        # Charge points to the agency operator rather than the client account.
        # Has no effect for direct accounts.
        if self._settings.yandex_client_login:
            headers["Use-Operator-Units"] = "true"
        return headers

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self._client.aclose()

    @property
    def last_units(self) -> UnitsInfo | None:
        """Points status after the most recent call (for rate limit awareness)."""
        return self._last_units

    async def call(
        self,
        service: str,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Invoke a Direct API method. Returns the `result` dict.

        Retries only on transient errors (network, 5xx, rate limit). Validation
        and auth errors raise immediately — no point retrying them.
        """
        body: dict[str, Any] = {"method": method, "params": params or {}}
        log = self._logger.bind(service=service, method=method)

        try:
            async for attempt in AsyncRetrying(
                retry=retry_if_exception_type((ApiTransientError, RateLimitError)),
                stop=stop_after_attempt(5),
                wait=wait_random_exponential(multiplier=1.0, max=30.0),
                reraise=True,
            ):
                with attempt:
                    return await self._do_call(service, body, log)
        except RetryError as exc:  # pragma: no cover - tenacity reraise path
            raise exc.last_attempt.exception() or YaDirectError("retry exhausted") from exc

        # Unreachable, but keeps mypy happy.
        raise YaDirectError("retry loop returned without result")

    async def _do_call(
        self,
        service: str,
        body: dict[str, Any],
        log: structlog.stdlib.BoundLogger,
    ) -> dict[str, Any]:
        try:
            response = await self._client.post(f"/{service}", json=body)
        except httpx.TimeoutException as exc:
            log.warning("http.timeout", error=str(exc))
            raise ApiTransientError(f"timeout calling {service}") from exc
        except httpx.TransportError as exc:
            log.warning("http.transport_error", error=str(exc))
            raise ApiTransientError(f"transport error: {exc}") from exc

        self._last_units = UnitsInfo.parse(response.headers.get("Units"))
        if self._last_units:
            log = log.bind(
                units_cost=self._last_units.last_cost,
                units_remaining=self._last_units.remaining,
            )

        if response.status_code >= 500:
            log.warning("http.server_error", status=response.status_code)
            raise ApiTransientError(f"HTTP {response.status_code}")

        try:
            payload: dict[str, Any] = response.json()
        except ValueError as exc:
            raise ApiTransientError(f"non-JSON response: {response.text[:200]!r}") from exc

        if "error" in payload:
            self._raise_for_error(payload["error"], log)

        log.info("api.call_ok")
        return payload.get("result", {})  # type: ignore[no-any-return]

    @staticmethod
    def _raise_for_error(err: dict[str, Any], log: structlog.stdlib.BoundLogger) -> None:
        code = int(err.get("error_code", 0))
        message = str(err.get("error_string", "Unknown error"))
        detail = err.get("error_detail")
        request_id = err.get("request_id")

        log.error(
            "api.error",
            code=code,
            message=message,
            detail=detail,
            request_id=request_id,
        )

        kwargs: dict[str, Any] = {"code": code, "request_id": request_id, "detail": detail}

        if code in _AUTH_CODES:
            raise AuthError(message, **kwargs)
        if code in _QUOTA_CODES:
            raise QuotaExceededError(message, **kwargs)
        if code in _RATE_LIMIT_CODES:
            raise RateLimitError(message, **kwargs)
        if code in _VALIDATION_CODES:
            raise ValidationError(message, **kwargs)
        raise YaDirectError(message, **kwargs)


# --- Convenience: tenacity retrier for Metrika/Wordstat clients that want the
# same retry semantics without the Direct-specific error mapping.


def make_simple_retrier() -> AsyncRetrying:
    return AsyncRetrying(
        retry=retry_if_exception_type((httpx.TimeoutException, httpx.TransportError)),
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1.0, max=20.0),
        reraise=True,
    )
