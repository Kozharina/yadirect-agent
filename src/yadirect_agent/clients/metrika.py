"""Yandex Metrika API client (M6 basic).

Metrika is REST/JSON, not JSON-RPC like Direct, so this client looks
different from ``clients/base.py`` — different transport idioms, but
the same project-wide guarantees:

- ``async with`` lifecycle (no leaked connections)
- Bearer-style auth with the Metrika OAuth token from Settings
- Retry with exponential backoff on transient failures (429, 5xx,
  network timeouts) — capped at 4 attempts so a dead Metrika doesn't
  hang the agent loop indefinitely
- Typed exceptions: AuthError (401/403), ValidationError (4xx other
  than 401/403/429), RateLimitError (429 once retries exhaust),
  ApiTransientError (5xx once retries exhaust). Same hierarchy the
  Direct client uses so the agent and services don't need
  source-aware error handling.

Methods covered in this iteration (M6 basic):

- ``get_goals(counter_id)`` — list goals on a counter for the
  reporting service.
- ``get_report(...)`` — generic /stat/v1/data wrapper. Implemented
  in the next commit.
- ``get_conversion_by_source(...)`` — specialised report for
  ``conversion_integrity`` checks. Next commit.

Out of scope here: management write methods (M9 audiences will need
some of those), counter creation, the Logs API. Each is a separate
PR with its own safety review.
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
)

from ..config import Settings
from ..exceptions import (
    ApiTransientError,
    AuthError,
    RateLimitError,
    ValidationError,
)
from ..models.metrika import DateRange, MetrikaGoal, ReportRow

_log = structlog.get_logger(component="clients.metrika")


# Status codes that warrant a retry. 429 is included so we honour
# Metrika's rate-limit signal; 5xx covers transient server issues.
# 408 (request timeout) is treated as transient too.
_RETRYABLE_STATUSES: frozenset[int] = frozenset({408, 429, 500, 502, 503, 504})


class _RetryableStatus(Exception):  # noqa: N818 -- control-flow signal, not an error
    """Internal signal: this HTTP response should be retried.

    Wraps the response so the retry loop can re-emit a typed terminal
    error (RateLimitError / ApiTransientError) once the budget is spent.
    Never escapes this module.
    """

    def __init__(self, response: httpx.Response) -> None:
        super().__init__(f"retryable status {response.status_code}")
        self.response = response


def _extract_message(response: httpx.Response) -> str:
    """Best-effort message extraction from a Metrika error envelope.

    Metrika returns ``{"errors": [{"message": "...", "error_type": "..."}]}``
    on errors. We pull the first error's message; if the body isn't JSON
    or the shape is unexpected, fall back to the raw text so the audit
    log still has something useful instead of just a status code.
    """
    try:
        body = response.json()
    except (ValueError, TypeError):
        return response.text or f"HTTP {response.status_code}"

    if isinstance(body, dict) and isinstance(body.get("errors"), list) and body["errors"]:
        first = body["errors"][0]
        if isinstance(first, dict) and "message" in first:
            return str(first["message"])
    return response.text or f"HTTP {response.status_code}"


def _classify_terminal(response: httpx.Response) -> Exception:
    """Map a non-retryable / retry-exhausted response to a typed error.

    Called either on the first non-retryable failure, or when retries
    have been exhausted on a retryable status. Caller raises the result.
    """
    msg = _extract_message(response)
    status = response.status_code
    if status in (401, 403):
        return AuthError(msg, code=status)
    if status == 429:
        return RateLimitError(msg, code=status)
    if 500 <= status < 600:
        return ApiTransientError(msg, code=status)
    # Everything else 4xx that wasn't 401/403/429 is a request-level
    # validation problem — bad params, unknown counter, etc.
    return ValidationError(msg, code=status)


class MetrikaService:
    """Thin HTTP client for Yandex Metrika.

    Lifecycle is async-context: the underlying ``httpx.AsyncClient`` is
    created in ``__aenter__`` and closed in ``__aexit__`` so callers
    can't leak connections by forgetting cleanup.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        # Client is constructed lazily in __aenter__ to keep object
        # construction side-effect-free (matches DirectService idiom).
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> Self:
        self._client = httpx.AsyncClient(
            base_url=self._settings.metrika_base_url,
            timeout=30.0,
            headers={
                "Authorization": (
                    f"OAuth {self._settings.yandex_metrika_token.get_secret_value()}"
                ),
                "Accept": "application/json",
            },
        )
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _require_client(self) -> httpx.AsyncClient:
        if self._client is None:
            msg = "MetrikaService used outside of `async with` context"
            raise RuntimeError(msg)
        return self._client

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """Send a request with retry on transient failures.

        Retries on:
        - status codes in ``_RETRYABLE_STATUSES`` (429, 5xx, 408);
        - ``httpx.TimeoutException`` and ``httpx.TransportError``
          (read/connect/network glitches).

        Stops after 4 attempts total; ``RetryError`` from tenacity is
        unwrapped to a typed terminal error pointing at the last
        response so callers see a stable exception type.
        """
        client = self._require_client()

        async def _attempt() -> httpx.Response:
            response = await client.request(method, path, params=params)
            if response.status_code in _RETRYABLE_STATUSES:
                raise _RetryableStatus(response)
            return response

        retrier = AsyncRetrying(
            retry=retry_if_exception_type(
                (_RetryableStatus, httpx.TimeoutException, httpx.TransportError),
            ),
            stop=stop_after_attempt(4),
            wait=wait_exponential(multiplier=0.1, max=2.0),
            reraise=True,
        )

        try:
            async for attempt in retrier:
                with attempt:
                    return await _attempt()
        except RetryError as exc:  # pragma: no cover -- reraise=True bypasses this
            raise ApiTransientError("retries exhausted") from exc
        except _RetryableStatus as final:
            # Retries exhausted on a retryable HTTP status. Promote to
            # a typed terminal error using the last response we saw.
            raise _classify_terminal(final.response) from None

        msg = "unreachable: retrier produced no result"
        raise RuntimeError(msg)  # pragma: no cover

    async def get_counters(self) -> list[dict[str, Any]]:
        """List counters this token has access to. Used by the doctor command."""
        response = await self._request("GET", "/management/v1/counters")
        if response.status_code != 200:
            raise _classify_terminal(response)
        return list(response.json().get("counters", []))

    async def get_goals(self, *, counter_id: int) -> list[MetrikaGoal]:
        """List goals defined on the given counter.

        Maps GET /management/v1/counter/{counter_id}/goals.

        Returns an empty list if the counter has no goals (a brand-new
        counter on a freshly installed pixel will hit this); never
        returns None so callers can iterate without a guard.

        Raises:
            AuthError: token invalid or lacks the metrika:read scope.
            ValidationError: counter_id rejected (not numeric, not
                accessible from this token).
            ApiTransientError: 5xx with retries exhausted.
            RateLimitError: 429 with retries exhausted.
        """
        response = await self._request(
            "GET",
            f"/management/v1/counter/{counter_id}/goals",
        )
        if response.status_code != 200:
            raise _classify_terminal(response)
        body = response.json()
        raw_goals = body.get("goals", [])
        return [MetrikaGoal.model_validate(g) for g in raw_goals]

    async def get_report(
        self,
        *,
        counter_id: int,
        metrics: list[str],
        dimensions: list[str],
        date_range: DateRange,
        filters: str | None = None,
    ) -> list[ReportRow]:
        """Fetch a stat report.

        Maps GET /stat/v1/data with comma-separated metrics/dimensions
        and ISO date1/date2.

        ``metrics`` and ``dimensions`` are passed verbatim — the caller
        composes valid Metrika identifiers (``ym:s:visits``,
        ``ym:ad:directCost``, ``ym:s:goal<id>conversions``, etc.).
        We don't enum-validate them here because Metrika has hundreds
        of metrics and the surface evolves; an invalid one returns 400,
        which we map to ValidationError.

        ``filters`` is passed through verbatim if non-None (Metrika's
        own filter language). When None we omit the query parameter
        entirely — Metrika rejects an empty ``filters=`` value.

        Returns an empty list when the report has no matching data.

        Raises:
            AuthError: token invalid or lacks scope.
            ValidationError: invalid metric / dimension / filter, or
                date range out of allowed bounds.
            ApiTransientError: 5xx with retries exhausted.
            RateLimitError: 429 with retries exhausted.
        """
        date1, date2 = date_range.to_metrika_strings()
        params: dict[str, Any] = {
            "ids": str(counter_id),
            "metrics": ",".join(metrics),
            "dimensions": ",".join(dimensions),
            "date1": date1,
            "date2": date2,
        }
        if filters is not None:
            params["filters"] = filters

        response = await self._request("GET", "/stat/v1/data", params=params)
        if response.status_code != 200:
            raise _classify_terminal(response)
        body = response.json()
        rows = body.get("data", [])
        return [ReportRow.model_validate(r) for r in rows]

    async def get_conversion_by_source(
        self,
        *,
        counter_id: int,
        goal_id: int,
        date_range: DateRange,
    ) -> dict[str, int]:
        """Conversions for one goal, broken down by traffic source.

        Specialised wrapper around ``get_report`` that:
        - asks for ``ym:s:goal<goal_id>conversions`` (goal-specific,
          NOT total conversions — picking the wrong metric here would
          silently route budget decisions toward an unrelated goal),
        - groups by ``ym:s:lastDirectClickSourceName``.

        Returns a ``{source_name: conversion_count}`` mapping.
        Counts are converted to ``int`` because the Metrika wire format
        is float (``5.0``) but conversions are inherently integer; we
        truncate the noise at the boundary so callers don't have to.

        Returns an empty dict when no conversions occurred in the
        window.
        """
        rows = await self.get_report(
            counter_id=counter_id,
            metrics=[f"ym:s:goal{goal_id}conversions"],
            dimensions=["ym:s:lastDirectClickSourceName"],
            date_range=date_range,
        )
        result: dict[str, int] = {}
        for row in rows:
            if not row.dimensions or not row.metrics:
                continue
            source_name = row.dimensions[0].get("name")
            if not isinstance(source_name, str):
                continue
            result[source_name] = int(row.metrics[0])
        return result
