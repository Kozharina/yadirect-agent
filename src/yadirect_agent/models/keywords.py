"""Pydantic models for keywords and bids."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class Keyword(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")

    id: int | None = Field(None, alias="Id")
    ad_group_id: int | None = Field(None, alias="AdGroupId")
    keyword: str = Field(..., alias="Keyword")
    state: str | None = Field(None, alias="State")
    status: str | None = Field(None, alias="Status")


class KeywordBid(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    keyword_id: int = Field(..., alias="KeywordId")
    # Bids are in micro-currency units (RUB * 1_000_000).
    search_bid: int | None = Field(None, alias="SearchBid")
    network_bid: int | None = Field(None, alias="NetworkBid")
