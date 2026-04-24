"""Tests for BiddingService.

Backfill for an existing contract — the code already does rubles→micro
conversion and empty-list no-op; these tests pin that contract so a
regression shows up as red, not as a silent misbehaviour on a live
account. No behaviour change here, exempt per
docs/TESTING.md #what_counts_as_behaviour_change.

Strategy: monkeypatch DirectService (same pattern as test_campaigns.py)
so no HTTP is touched. We observe what arrives at `set_keyword_bids`
and assert on it.
"""

from __future__ import annotations

from typing import Any

import pytest

from yadirect_agent.clients import direct as direct_module
from yadirect_agent.config import Settings
from yadirect_agent.models.keywords import KeywordBid
from yadirect_agent.services.bidding import BiddingService, BidUpdate

# --------------------------------------------------------------------------
# In-memory DirectService stub.
# --------------------------------------------------------------------------


class _FakeDirectService:
    """Captures the payload that reaches set_keyword_bids."""

    def __init__(self) -> None:
        self.set_keyword_bids_calls: list[list[KeywordBid]] = []

    async def __aenter__(self) -> _FakeDirectService:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    async def set_keyword_bids(self, bids: list[KeywordBid]) -> dict[str, Any]:
        # Copy so later mutations in the service don't rewrite captures.
        self.set_keyword_bids_calls.append(list(bids))
        return {}


@pytest.fixture
def fake_direct(monkeypatch: pytest.MonkeyPatch) -> _FakeDirectService:
    fake = _FakeDirectService()

    def _factory(_settings: Settings) -> _FakeDirectService:
        return fake

    # Patch both the source module and the site where BiddingService imports
    # DirectService — same gotcha as in tests/unit/services/test_campaigns.py.
    monkeypatch.setattr("yadirect_agent.services.bidding.DirectService", _factory)
    monkeypatch.setattr(direct_module, "DirectService", _factory)
    return fake


# --------------------------------------------------------------------------
# apply(): empty list short-circuit.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_empty_list_is_a_noop(
    settings: Settings, fake_direct: _FakeDirectService
) -> None:
    # Empty list must not reach the API at all — we don't want to pay the
    # network round-trip or a units charge for nothing.
    await BiddingService(settings).apply([])

    assert fake_direct.set_keyword_bids_calls == []


# --------------------------------------------------------------------------
# apply(): rubles → micro-currency conversion.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_single_search_bid_converts_rubles_to_micro(
    settings: Settings, fake_direct: _FakeDirectService
) -> None:
    # 10.50 RUB → 10_500_000 micro.
    await BiddingService(settings).apply([BidUpdate(keyword_id=42, new_search_bid_rub=10.50)])

    [[bid]] = fake_direct.set_keyword_bids_calls
    assert bid.keyword_id == 42
    assert bid.search_bid == 10_500_000
    assert bid.network_bid is None


@pytest.mark.asyncio
async def test_apply_single_network_bid_converts_rubles_to_micro(
    settings: Settings, fake_direct: _FakeDirectService
) -> None:
    await BiddingService(settings).apply([BidUpdate(keyword_id=7, new_network_bid_rub=3.0)])

    [[bid]] = fake_direct.set_keyword_bids_calls
    assert bid.keyword_id == 7
    assert bid.search_bid is None
    assert bid.network_bid == 3_000_000


@pytest.mark.asyncio
async def test_apply_both_search_and_network_bids(
    settings: Settings, fake_direct: _FakeDirectService
) -> None:
    await BiddingService(settings).apply(
        [BidUpdate(keyword_id=1, new_search_bid_rub=5.0, new_network_bid_rub=2.0)]
    )

    [[bid]] = fake_direct.set_keyword_bids_calls
    assert bid.search_bid == 5_000_000
    assert bid.network_bid == 2_000_000


@pytest.mark.asyncio
async def test_apply_with_both_bids_none_passes_nones_through(
    settings: Settings, fake_direct: _FakeDirectService
) -> None:
    # Degenerate input: a keyword update with no actual bid set. Service
    # doesn't validate this today; the client will ignore it server-side.
    # Pinning so we notice if that becomes a service-level error later.
    await BiddingService(settings).apply([BidUpdate(keyword_id=1)])

    [[bid]] = fake_direct.set_keyword_bids_calls
    assert bid.keyword_id == 1
    assert bid.search_bid is None
    assert bid.network_bid is None


# --------------------------------------------------------------------------
# apply(): batching.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_batches_multiple_updates_into_one_call(
    settings: Settings, fake_direct: _FakeDirectService
) -> None:
    # The service sends all updates to the API in a single request — we
    # do not want one keyword per HTTP call (that would burn Units quota
    # and make the audit trail illegible).
    updates = [
        BidUpdate(keyword_id=1, new_search_bid_rub=1.0),
        BidUpdate(keyword_id=2, new_search_bid_rub=2.0),
        BidUpdate(keyword_id=3, new_network_bid_rub=3.0),
    ]

    await BiddingService(settings).apply(updates)

    assert len(fake_direct.set_keyword_bids_calls) == 1
    batch = fake_direct.set_keyword_bids_calls[0]
    assert [b.keyword_id for b in batch] == [1, 2, 3]
    assert batch[0].search_bid == 1_000_000
    assert batch[1].search_bid == 2_000_000
    assert batch[2].network_bid == 3_000_000
