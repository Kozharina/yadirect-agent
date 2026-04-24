"""Tests for SemanticsService.

Strategy:
- `normalize` and `_cluster_key` are pure — tested directly with inputs.
- `collect` and `validate_with_direct` take a WordstatProvider (Protocol).
  We inject a small fake (no HTTP, no SDK import) per test; see
  docs/TESTING.md #service_layer_tests for why this is preferred over
  `respx` here.

The first test in this file is the RED that drives a fix in
_cluster_key's all-stop-words fallback — see commit history.
"""

from __future__ import annotations

from typing import Any

import pytest

from yadirect_agent.clients.wordstat import WordstatProvider
from yadirect_agent.services.semantics import KeywordCluster, SemanticsService

# --------------------------------------------------------------------------
# In-memory Wordstat fake.
# --------------------------------------------------------------------------


class _FakeWordstat:
    """Scripted WordstatProvider: return pre-baked responses, record calls."""

    def __init__(
        self,
        *,
        expand: list[dict[str, Any]] | None = None,
        presence: dict[str, bool] | None = None,
    ) -> None:
        self._expand = expand or []
        self._presence = presence or {}
        self.expand_calls: list[tuple[list[str], list[int] | None]] = []
        self.presence_calls: list[tuple[list[str], list[int] | None]] = []

    async def has_search_volume(self, phrases: list[str], geo: list[int] | None) -> dict[str, bool]:
        self.presence_calls.append((list(phrases), geo))
        # Default to False if provider doesn't know the phrase.
        return {p: self._presence.get(p, False) for p in phrases}

    async def expand_seeds(self, seeds: list[str], geo: list[int] | None) -> list[dict[str, Any]]:
        self.expand_calls.append((list(seeds), geo))
        return list(self._expand)


def _svc(fake: WordstatProvider) -> SemanticsService:
    return SemanticsService(fake)


# --------------------------------------------------------------------------
# _cluster_key: the all-stop-words fallback contract (RED).
# --------------------------------------------------------------------------


class TestClusterKeyAllStopWordsFallback:
    """When every token is a stop word, _cluster_key falls back to the
    input phrase. The phrase returned must still be normalised (lowercase,
    collapsed whitespace) — the same contract the happy path honours.
    The current implementation returns the raw input, which breaks that
    contract; this class's tests pin the fix.
    """

    def test_returns_lowercase_for_all_upper_case_stop_words(self) -> None:
        # All three are stop words; caller should still get a normalised key.
        # Cyrillic letters are intentional (RUF001 would flag the mixed-case RU word).
        assert SemanticsService._cluster_key("В И На") == "в и на"  # noqa: RUF001

    def test_collapses_whitespace_in_fallback(self) -> None:
        # Stop-only phrase with stray whitespace — still a valid cluster key,
        # must not carry the original spacing.
        assert SemanticsService._cluster_key("  в  и  ") == "в и"


# --------------------------------------------------------------------------
# normalize: pure helper.
# --------------------------------------------------------------------------


class TestNormalize:
    def test_strips_leading_and_trailing_whitespace(self) -> None:
        assert SemanticsService.normalize("  foo  ") == "foo"

    def test_collapses_internal_whitespace_runs(self) -> None:
        assert SemanticsService.normalize("foo    bar\tbaz") == "foo bar baz"

    def test_lowercases(self) -> None:
        assert SemanticsService.normalize("Купить ОБУВЬ") == "купить обувь"

    def test_empty_string_is_empty(self) -> None:
        assert SemanticsService.normalize("") == ""

    def test_whitespace_only_becomes_empty(self) -> None:
        assert SemanticsService.normalize("   \t  ") == ""


# --------------------------------------------------------------------------
# _cluster_key: happy paths.
# --------------------------------------------------------------------------


class TestClusterKeyHappy:
    def test_sorts_content_tokens(self) -> None:
        # Order-insensitive: "купить обувь" and "обувь купить" share a key.
        assert SemanticsService._cluster_key("купить обувь") == SemanticsService._cluster_key(
            "обувь купить"
        )

    def test_drops_stop_words(self) -> None:
        # "в" is a stop word; its presence should not affect the key.
        assert SemanticsService._cluster_key(
            "купить обувь в москве"
        ) == SemanticsService._cluster_key("купить обувь москве")

    def test_case_insensitive(self) -> None:
        assert SemanticsService._cluster_key("КУПИТЬ обувь") == SemanticsService._cluster_key(
            "купить Обувь"
        )

    def test_single_content_word(self) -> None:
        assert SemanticsService._cluster_key("Обувь") == "обувь"


# --------------------------------------------------------------------------
# collect: clustering + sort-by-shows.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
class TestCollect:
    async def test_groups_phrases_sharing_a_cluster_key(self) -> None:
        fake = _FakeWordstat(
            expand=[
                {"phrase": "купить обувь", "shows": 100},
                # Re-orderings and case variants collapse into the same key.
                {"phrase": "Обувь купить", "shows": 50},
                {"phrase": "обувь в москве", "shows": 20},
            ]
        )

        clusters = await _svc(fake).collect(seeds=["обувь"])

        # Two distinct clusters: {купить, обувь} and {москве, обувь}.
        assert len(clusters) == 2
        assert sum(len(c.phrases) for c in clusters) == 3

    async def test_sorts_clusters_by_total_shows_descending(self) -> None:
        fake = _FakeWordstat(
            expand=[
                {"phrase": "купить обувь", "shows": 100},
                {"phrase": "обувь купить", "shows": 50},  # same cluster, 150 total
                {"phrase": "обувь москва", "shows": 500},  # different cluster
            ]
        )

        clusters = await _svc(fake).collect(seeds=["обувь"])

        # "москва обувь" (shows=500) must come before "купить обувь" (150).
        assert clusters[0].total_shows == 500
        assert clusters[1].total_shows == 150

    async def test_normalises_and_drops_empty_seeds(self) -> None:
        fake = _FakeWordstat(expand=[])

        await _svc(fake).collect(seeds=["  foo  ", "", "BAR"])

        # The provider sees normalised, non-empty seeds only.
        assert fake.expand_calls == [(["foo", "bar"], None)]

    async def test_passes_geo_through_to_provider(self) -> None:
        fake = _FakeWordstat(expand=[])

        await _svc(fake).collect(seeds=["foo"], geo=[213])

        assert fake.expand_calls[0][1] == [213]

    async def test_returns_empty_list_when_provider_returns_nothing(self) -> None:
        fake = _FakeWordstat(expand=[])

        assert await _svc(fake).collect(seeds=["foo"]) == []

    async def test_tolerates_missing_shows_field(self) -> None:
        # Spec says shows is an int; be liberal in what we accept at the edge.
        fake = _FakeWordstat(
            expand=[{"phrase": "foo bar"}]  # no 'shows' key
        )

        clusters = await _svc(fake).collect(seeds=["foo"])

        assert len(clusters) == 1
        assert clusters[0].total_shows == 0
        assert isinstance(clusters[0], KeywordCluster)


# --------------------------------------------------------------------------
# validate_with_direct.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
class TestValidateWithDirect:
    async def test_keeps_phrases_the_provider_says_have_volume(self) -> None:
        fake = _FakeWordstat(presence={"купить обувь": True, "заказать что попало": False})

        kept = await _svc(fake).validate_with_direct(["купить обувь", "заказать что попало"])

        assert kept == ["купить обувь"]

    async def test_returns_empty_list_when_nothing_has_volume(self) -> None:
        fake = _FakeWordstat(presence={})

        kept = await _svc(fake).validate_with_direct(["a", "b"])

        assert kept == []

    async def test_calls_provider_with_none_geo(self) -> None:
        # Today's contract: validate_with_direct does not accept geo; it's
        # always passed as None. Pinning so a later signature change is an
        # intentional red.
        fake = _FakeWordstat(presence={"x": True})

        await _svc(fake).validate_with_direct(["x"])

        assert fake.presence_calls == [(["x"], None)]
