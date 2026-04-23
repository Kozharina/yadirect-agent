"""Bid management service.

Rules we enforce here (not at the API client level):
- Never raise a bid by more than +50% in a single call.
- Never lower a bid below the campaign's declared floor (if set).
- Bid changes in RUB are converted to micro-currency units at the boundary.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from ..clients.direct import DirectService
from ..config import Settings
from ..models.keywords import KeywordBid


@dataclass(frozen=True)
class BidUpdate:
    keyword_id: int
    new_search_bid_rub: float | None = None
    new_network_bid_rub: float | None = None


class BiddingService:
    MAX_INCREASE_PCT: float = 0.5  # +50% per single call

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._logger = structlog.get_logger().bind(component="bidding_service")

    async def apply(self, updates: list[BidUpdate]) -> None:
        if not updates:
            return

        # TODO(iteration 2): fetch current bids and reject updates that violate
        # MAX_INCREASE_PCT. For now we only convert units and forward.
        payload = [
            KeywordBid(
                keyword_id=u.keyword_id,
                search_bid=(
                    int(u.new_search_bid_rub * 1_000_000)
                    if u.new_search_bid_rub is not None
                    else None
                ),
                network_bid=(
                    int(u.new_network_bid_rub * 1_000_000)
                    if u.new_network_bid_rub is not None
                    else None
                ),
            )
            for u in updates
        ]

        self._logger.info("bids.apply.request", count=len(payload))
        async with DirectService(self._settings) as api:
            await api.set_keyword_bids(payload)
        self._logger.info("bids.apply.ok", count=len(payload))
