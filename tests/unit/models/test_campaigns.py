"""Tests for the Campaign pydantic model.

Pins the Direct API row → safety-snapshot path for KS#3
(negative-keyword floor): the ``Campaign`` model now exposes
campaign-level negative keywords as a flat list of phrases so
``CampaignBudget.negative_keywords`` can populate without a second
service-level fetch. Without this field, ``_build_resume_context``
hands KS#3 an empty set on every campaign, and once an operator
configures ``required_negative_keywords`` in agent_policy.yml every
resume blocks regardless of whether the campaign actually carries
the required phrases.
"""

from __future__ import annotations

from yadirect_agent.models.campaigns import Campaign


def test_campaign_parses_minimal_row_without_negatives_field() -> None:
    """A row that predates the NegativeKeywords addition still
    validates — the field defaults to an empty list. Protects
    existing fixtures and any cassette-style tests that don't
    surface the new field."""
    c = Campaign.model_validate(
        {
            "Id": 1,
            "Name": "c1",
            "State": "ON",
            "Status": "ACCEPTED",
        }
    )
    assert c.id == 1
    assert c.negative_keywords == []


def test_campaign_extracts_negative_keywords_items() -> None:
    """Direct returns ``NegativeKeywords`` as ``{"Items": [...]}``.
    The model flattens to a plain ``list[str]`` so the safety layer
    (which works in plain phrases) doesn't have to know about the
    envelope shape."""
    c = Campaign.model_validate(
        {
            "Id": 1,
            "Name": "c1",
            "NegativeKeywords": {"Items": ["бесплатно", "скачать", "отзывы"]},
        }
    )
    assert c.negative_keywords == ["бесплатно", "скачать", "отзывы"]


def test_campaign_handles_empty_negative_keywords_envelope() -> None:
    """Direct may return ``NegativeKeywords: {"Items": []}`` for a
    campaign without negatives — distinct from the field being
    absent altogether. Both must collapse to ``[]`` so KS#3 can
    treat them uniformly (a campaign with no negatives configured
    fails the floor check the same way as one whose negatives field
    wasn't requested)."""
    c = Campaign.model_validate(
        {
            "Id": 1,
            "Name": "c1",
            "NegativeKeywords": {"Items": []},
        }
    )
    assert c.negative_keywords == []


def test_campaign_handles_null_negative_keywords_field() -> None:
    """Defensive: some Direct API responses omit ``Items`` or send
    ``NegativeKeywords: null`` for campaigns where the operator
    hasn't touched negatives. Treat as empty rather than crashing
    the model_validate path on an absent ``Items`` key."""
    c = Campaign.model_validate(
        {
            "Id": 1,
            "Name": "c1",
            "NegativeKeywords": None,
        }
    )
    assert c.negative_keywords == []
