"""Semantic core collection and clustering.

Iteration 1: in-memory normalization + clustering by lemma-overlap.
Iteration 2: plug in a real Wordstat provider (see clients/wordstat.py);
            add clustering by SERP similarity or embeddings.

We keep the API provider-agnostic: feed in phrases, get back cleaned clusters.
The provider is injected, not imported here — that's what makes the module
swappable.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field

import structlog

from ..clients.wordstat import WordstatProvider

_WHITESPACE_RE = re.compile(r"\s+")
_STOP_WORDS: frozenset[str] = frozenset(
    # Minimal RU stop list for cluster-keying. Expand in iteration 2.
    # Cyrillic letters are intentional — RUF001 would flag otherwise.
    {"в", "и", "на", "с", "от", "для", "по", "из", "к", "у", "о", "за", "до"}  # noqa: RUF001
)


@dataclass
class KeywordCluster:
    key: str
    phrases: list[str] = field(default_factory=list)
    total_shows: int = 0


class SemanticsService:
    def __init__(self, wordstat: WordstatProvider) -> None:
        self._wordstat = wordstat
        self._logger = structlog.get_logger().bind(component="semantics")

    @staticmethod
    def normalize(phrase: str) -> str:
        return _WHITESPACE_RE.sub(" ", phrase.strip().lower())

    @classmethod
    def _cluster_key(cls, phrase: str) -> str:
        """Naive clustering: sorted content words. Replace with embeddings later."""
        tokens = [t for t in cls.normalize(phrase).split() if t not in _STOP_WORDS]
        tokens.sort()
        return " ".join(tokens) if tokens else phrase

    async def collect(self, seeds: list[str], geo: list[int] | None = None) -> list[KeywordCluster]:
        seeds = [self.normalize(s) for s in seeds if s.strip()]
        self._logger.info("semantics.collect.start", seed_count=len(seeds))

        expanded = await self._wordstat.expand_seeds(seeds, geo)
        clusters: dict[str, KeywordCluster] = defaultdict(lambda: KeywordCluster(key=""))
        for item in expanded:
            phrase = self.normalize(item["phrase"])
            key = self._cluster_key(phrase)
            c = clusters[key]
            if not c.key:
                c.key = key
            c.phrases.append(phrase)
            c.total_shows += int(item.get("shows", 0))

        result = sorted(clusters.values(), key=lambda c: c.total_shows, reverse=True)
        self._logger.info("semantics.collect.ok", cluster_count=len(result))
        return result

    async def validate_with_direct(self, phrases: list[str]) -> list[str]:
        """Keep only phrases that Direct confirms have search volume."""
        presence = await self._wordstat.has_search_volume(phrases, None)
        return [p for p, has in presence.items() if has]
