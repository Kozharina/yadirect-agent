"""Wordstat client (stub — iteration 2).

IMPORTANT: Yandex Wordstat is NOT part of the Direct API. It has its own
separate access paths:

1. **Direct API 'keywordsresearch'** (limited)
   - Method: keywordsresearch.hasSearchVolume / .deserializeRequest
   - Scope: `direct:api` (same token as Direct)
   - Limitation: quota-heavy, blunt — not a real Wordstat replacement.

2. **Wordstat API** (https://api.wordstat.yandex.net)
   - Separate registration and approval. Russian entity typically required.
   - Strict daily request quota.
   - Returns impressions by phrase + related queries.

3. **Third-party (Key Collector / Bukvarix / Topvisor / etc.)**
   - Production-grade semantic collection.
   - Paid, but robust. Most Russian PPC agencies use one of these.

For iteration 1 we'll implement a minimal Direct-side lookup for
`hasSearchVolume` checks and leave a clean abstraction so we can slot in any
of the above later.
"""

from __future__ import annotations

from typing import Any, Protocol

from ..config import Settings
from .direct import DirectService


class WordstatProvider(Protocol):
    """Pluggable interface — so we can swap Direct / Wordstat API / third-party."""

    async def has_search_volume(
        self, phrases: list[str], geo: list[int] | None
    ) -> dict[str, bool]: ...

    async def expand_seeds(self, seeds: list[str], geo: list[int] | None) -> list[dict[str, Any]]:
        """Return [{'phrase': str, 'shows': int, 'source': str}, ...]."""
        ...


class DirectKeywordsResearch:
    """Minimal Wordstat-ish functionality through the Direct API.

    Only implements `has_search_volume`. `expand_seeds` raises — use a real
    Wordstat provider for that.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def has_search_volume(
        self, phrases: list[str], geo: list[int] | None = None
    ) -> dict[str, bool]:
        async with DirectService(self._settings) as svc:
            result = await svc._api.call(
                "keywordsresearch",
                "hasSearchVolume",
                {
                    "Phrases": phrases,
                    "GeoIds": geo or [],
                },
            )
        items: list[dict[str, Any]] = result.get("HasSearchVolumeItems", [])
        return {item["Phrase"]: bool(item.get("HasSearchVolume")) for item in items}

    async def expand_seeds(
        self, seeds: list[str], geo: list[int] | None = None
    ) -> list[dict[str, Any]]:
        raise NotImplementedError(
            "Direct API does not expose a phrase-expansion method. "
            "Plug in Wordstat API or a third-party provider."
        )
