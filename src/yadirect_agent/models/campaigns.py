"""Pydantic models for campaign-related API resources.

Field names match Direct API v5 verbatim (PascalCase) so we can round-trip
without custom aliases everywhere. For internal Python code we still use
snake_case via `alias` where needed.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CampaignState(StrEnum):
    ON = "ON"
    OFF = "OFF"
    SUSPENDED = "SUSPENDED"
    ENDED = "ENDED"
    CONVERTED = "CONVERTED"
    ARCHIVED = "ARCHIVED"


class CampaignStatus(StrEnum):
    ACCEPTED = "ACCEPTED"
    DRAFT = "DRAFT"
    MODERATION = "MODERATION"
    REJECTED = "REJECTED"


class DailyBudget(BaseModel):
    """Direct's per-campaign daily budget envelope.

    Accepts both the API's PascalCase keys (``Amount`` / ``Mode``,
    what ``campaigns.get`` actually returns) and the snake_case
    field names used by every test fixture and any future
    programmatic builder. Without these aliases ``Campaign.model_validate``
    on a real wire response would raise ValidationError on the
    inner ``amount`` field — silent in tests, loud in production
    on the first ``DirectService.get_campaigns`` call.
    """

    model_config = ConfigDict(populate_by_name=True)

    amount: int = Field(
        ...,
        alias="Amount",
        description="Budget in micro-currency units (RUB * 1_000_000)",
    )
    mode: str = Field(default="STANDARD", alias="Mode")  # STANDARD | DISTRIBUTED


class Campaign(BaseModel):
    """Minimal typed view of a campaign. Extend as more fields are needed."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    id: int = Field(..., alias="Id")
    name: str = Field(..., alias="Name")
    state: CampaignState | None = Field(None, alias="State")
    status: CampaignStatus | None = Field(None, alias="Status")
    type: str | None = Field(None, alias="Type")
    start_date: str | None = Field(None, alias="StartDate")
    end_date: str | None = Field(None, alias="EndDate")
    daily_budget: DailyBudget | None = Field(None, alias="DailyBudget")
    client_info: str | None = Field(None, alias="ClientInfo")
    # Direct returns campaign-level negatives as
    # ``"NegativeKeywords": {"Items": [...]}``. Flatten at the model
    # boundary so the safety layer (which works in plain phrases via
    # ``CampaignBudget.negative_keywords``) never sees the envelope.
    # Both an absent / null field and ``Items: []`` collapse to ``[]``
    # — KS#3 treats "no negatives" uniformly regardless of how Direct
    # rendered the empty case.
    negative_keywords: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _flatten_negative_keywords(cls, data: Any) -> Any:
        """Pull ``NegativeKeywords.Items`` up to the top-level
        ``negative_keywords`` field when the API envelope is present.

        Only fires when the ``NegativeKeywords`` key is explicitly in
        the input — direct construction via ``Campaign(negative_keywords=...)``
        is left untouched so test fixtures and any future code that
        builds a Campaign without the API envelope can still set the
        field directly.
        """
        if not isinstance(data, dict) or "NegativeKeywords" not in data:
            return data
        envelope = data["NegativeKeywords"]
        if envelope is None:
            data["negative_keywords"] = []
        elif isinstance(envelope, dict):
            items = envelope.get("Items") or []
            data["negative_keywords"] = list(items)
        return data
