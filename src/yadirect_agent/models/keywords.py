"""Pydantic models for keywords and bids."""

from __future__ import annotations

import math

from pydantic import BaseModel, ConfigDict, Field


class Productivity(BaseModel):
    """Direct's ``Productivity`` envelope on a keyword row.

    Surfaces only ``Value`` (a 0..10 quality score) — recommendations
    and other nested fields stay in ``model_extra`` for future use
    without a model migration. ``Value`` is documented as a numeric
    quality score; we type it as ``float | None`` and let the parent
    ``Keyword`` model normalise to ``int`` via the ``quality_score``
    property.
    """

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    value: float | None = Field(None, alias="Value")


class Keyword(BaseModel):
    """Keyword row returned by ``keywords.get``.

    Carries the safety-relevant Direct fields (``Bid``, ``ContextBid``,
    ``Productivity``) so ``BiddingService._build_bid_context`` can
    populate ``AccountBidSnapshot`` for KS#2 / KS#4 without a second
    adgroup-lookup round trip. Bids arrive in micro-currency (RUB *
    1_000_000) and are exposed as RUB via computed properties so the
    service layer never sees micro units.
    """

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    id: int | None = Field(None, alias="Id")
    ad_group_id: int | None = Field(None, alias="AdGroupId")
    campaign_id: int | None = Field(None, alias="CampaignId")
    keyword: str = Field(..., alias="Keyword")
    state: str | None = Field(None, alias="State")
    status: str | None = Field(None, alias="Status")
    # ``ge=0`` is load-bearing safety, not a sanity guard: a negative
    # bid would land in ``KeywordSnapshot.current_*_bid_rub`` and
    # poison KS#4's ``_is_increase(new, current)`` (positive new vs
    # negative current → reads as an increase even on a decrease) and
    # KS#2's cap arithmetic. Reject loudly at the model boundary —
    # the failure surfaces as a ValidationError on ``get_keywords``
    # rather than silently bypassing the safety pipeline. Auditor
    # M2-bid-snapshot HIGH-1.
    bid_micro: int | None = Field(None, alias="Bid", ge=0)
    context_bid_micro: int | None = Field(None, alias="ContextBid", ge=0)
    productivity: Productivity | None = Field(None, alias="Productivity")

    @property
    def current_search_bid_rub(self) -> float | None:
        """Search bid in RUB, or ``None`` when Direct didn't include
        ``Bid`` for this row. Zero is preserved as ``0.0`` — KS#4
        distinguishes 0 (real bid) from None (unknown, defer)."""
        if self.bid_micro is None:
            return None
        return self.bid_micro / 1_000_000

    @property
    def current_network_bid_rub(self) -> float | None:
        """Network bid in RUB, or ``None`` when Direct didn't include
        ``ContextBid`` for this row."""
        if self.context_bid_micro is None:
            return None
        return self.context_bid_micro / 1_000_000

    @property
    def quality_score(self) -> int | None:
        """Integer 0..10 from ``Productivity.Value``, rounded HALF UP.

        ``None`` when the envelope is absent, the ``Value`` is missing,
        or the value is outside 0..10 — all three cases become
        "unknown, defer" at KS#4 rather than crashing
        ``KeywordSnapshot.__post_init__``'s range guard.

        Rounding direction is load-bearing: ``min_quality_score_for_bid_increase``
        is an integer threshold and Python's built-in ``round`` uses
        banker's rounding (``round(4.5) == 4``), which would silently
        flip KS#4's verdict at exact .5 boundaries — a Direct row with
        ``Productivity.Value == 4.5`` (which Direct's own UI typically
        renders as 5) would block bid increases under banker's
        rounding but allow them under the operator's intuitive
        round-half-up. ``math.floor(value + 0.5)`` matches operator
        intuition and is pinned in tests/unit/models/test_keywords.py.
        Auditor M2-bid-snapshot MEDIUM.
        """
        if self.productivity is None or self.productivity.value is None:
            return None
        value = self.productivity.value
        if not 0 <= value <= 10:
            return None
        return math.floor(value + 0.5)


class KeywordBid(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    keyword_id: int = Field(..., alias="KeywordId")
    # Bids are in micro-currency units (RUB * 1_000_000).
    search_bid: int | None = Field(None, alias="SearchBid")
    network_bid: int | None = Field(None, alias="NetworkBid")
