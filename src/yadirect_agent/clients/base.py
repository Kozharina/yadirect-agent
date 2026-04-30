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
from .oauth import refresh_access_token

# Direct API auth error code that means "access token invalid /
# expired" — the only one of ``_AUTH_CODES`` (52, 53, 54, 58) where
# a refresh is meaningful. Codes 53 (header missing — our bug),
# 54 (no rights), and 58 (insufficient privileges) won't be fixed
# by a refresh and would mask the real cause if we tried.
_INVALID_TOKEN_CODE = 52

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

        One exception to that rule: ``AuthError`` with the
        ``invalid token`` code (52) triggers a one-shot OAuth
        refresh + retry. The keychain ``TokenSet`` (M15.3) carries
        the refresh_token; on success the new access_token is
        persisted to the keychain, mirrored into ``Settings``,
        and the httpx ``Authorization`` header is rewritten before
        the retry. A second AuthError on the retry surfaces as-is —
        no infinite loop. Codes 53 / 54 / 58 (header missing, no
        rights, insufficient privileges) are NOT refreshable and
        propagate immediately so the operator sees the real cause.
        """
        body: dict[str, Any] = {"method": method, "params": params or {}}
        log = self._logger.bind(service=service, method=method)

        try:
            return await self._call_with_transient_retries(service, body, log)
        except AuthError as exc:
            if exc.code != _INVALID_TOKEN_CODE:
                raise
            refreshed = await self._try_refresh_after_invalid_token(log)
            if not refreshed:
                raise
            # Retry exactly once with the fresh access token.
            # Whatever happens here surfaces — including a second
            # AuthError(52), which means the refresh worked but
            # the grant was revoked between refresh and retry.
            return await self._call_with_transient_retries(service, body, log)

    async def _call_with_transient_retries(
        self,
        service: str,
        body: dict[str, Any],
        log: structlog.stdlib.BoundLogger,
    ) -> dict[str, Any]:
        """Run ``_do_call`` inside the tenacity retry envelope.

        Extracted so the outer ``call`` can wrap the whole
        retry-exhausted result in the auth-refresh fallback
        without duplicating the tenacity glue. ``_do_call`` itself
        does NOT retry; tenacity is the single source of retry
        truth for transient classes.
        """
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

    async def _try_refresh_after_invalid_token(self, log: structlog.stdlib.BoundLogger) -> bool:
        """Attempt to refresh the keychain TokenSet.

        Returns True if the refresh succeeded and the httpx client
        + Settings are now using the new access token. Returns
        False if no refresh is possible (no keychain entry, or the
        refresh endpoint itself rejects). On False, the caller
        surfaces the original AuthError so the operator sees the
        actionable cause (re-run ``auth login``) rather than an
        opaque retry failure.

        Side effects on success:
        - New ``TokenSet`` persisted to keychain so the NEXT
          process invocation also benefits.
        - ``settings.yandex_direct_token`` and
          ``settings.yandex_metrika_token`` mirror the new
          access_token (same shape as
          ``Settings._hydrate_tokens_from_keyring``: a single
          OAuth grant covers both scopes).
        - ``self._client.headers["Authorization"]`` rewritten so
          the next HTTP call uses the fresh Bearer.
        """
        # Lazy import keeps the keyring stack out of every import
        # chain; the cost only pays on the (rare) refresh path.
        from ..auth.keychain import KeyringTokenStore

        store = KeyringTokenStore()
        token = store.load()
        if token is None:
            log.info("api.auth_refresh.skip_no_keychain_token")
            return False
        try:
            new_token = await refresh_access_token(
                refresh_token=token.refresh_token.get_secret_value(),
            )
        except Exception as exc:
            # Yandex OAuth refresh can fail in many ways — refresh
            # token expired, grant revoked at yandex.ru/profile/access,
            # transient network blip. None of them merit hiding the
            # original wire AuthError; we log the inner cause and
            # the caller surfaces the original error.
            log.warning("api.auth_refresh.failed", error=str(exc))
            return False

        store.save(new_token)
        self._settings.yandex_direct_token = new_token.access_token
        self._settings.yandex_metrika_token = new_token.access_token
        self._client.headers["Authorization"] = (
            f"Bearer {new_token.access_token.get_secret_value()}"
        )
        log.info("api.auth_refresh.ok")
        return True

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
