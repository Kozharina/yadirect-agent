"""Yandex Metrika API client (stub — iteration 2).

Design identical to DirectService. Base URL: https://api-metrika.yandex.net

Methods we'll need first:
- GET /management/v1/counters — list counters
- GET /management/v1/counter/{id}/goals — goals for a counter
- GET /stat/v1/data — fetch statistics

Metrika auth is a separate OAuth token with metrika:read/write scopes.
"""

from __future__ import annotations

from typing import Any, Self

import httpx

from ..config import Settings


class MetrikaService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = httpx.AsyncClient(
            base_url=settings.metrika_base_url,
            timeout=30.0,
            headers={"Authorization": f"OAuth {settings.yandex_metrika_token.get_secret_value()}"},
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self._client.aclose()

    async def get_counters(self) -> list[dict[str, Any]]:
        r = await self._client.get("/management/v1/counters")
        r.raise_for_status()
        return list(r.json().get("counters", []))

    # TODO(iteration 2):
    # - get_goals(counter_id)
    # - get_report(counter_id, metrics, dimensions, date_range)
    # - conversion_by_source(counter_id, date_range)
