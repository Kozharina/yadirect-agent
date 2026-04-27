"""Tests for the Keyword pydantic model.

Pins the Direct-API row → safety-snapshot path for KS#2 / KS#4: the
``Keyword`` model now carries the per-keyword bid (search + network)
and the Productivity envelope, and exposes them as RUB / int-QS via
computed properties so the service layer doesn't redo unit
conversion at every call site.

Without these fields, ``BiddingService._build_bid_context`` returns
an empty ``AccountBidSnapshot`` and KS#2 / KS#4 silently defer on
every bid update. See docs/BACKLOG.md "Per-keyword
AccountBidSnapshot reader for KS#2 / KS#4".
"""

from __future__ import annotations

import pytest

from yadirect_agent.models.keywords import Keyword

# --------------------------------------------------------------------------
# Backwards compat: existing-shape rows still parse.
# --------------------------------------------------------------------------


def test_keyword_parses_minimal_row_without_new_fields() -> None:
    """A row that predates the bid / productivity additions still
    validates — the new fields default to None. This protects the
    existing callers in ``DirectService.get_keywords`` whose tests
    feed minimal rows.
    """
    kw = Keyword.model_validate(
        {
            "Id": 1,
            "AdGroupId": 100,
            "Keyword": "купить обувь",
            "State": "ON",
            "Status": "ACCEPTED",
        }
    )
    assert kw.id == 1
    assert kw.ad_group_id == 100
    assert kw.campaign_id is None
    assert kw.keyword == "купить обувь"
    assert kw.bid_micro is None
    assert kw.context_bid_micro is None
    assert kw.current_search_bid_rub is None
    assert kw.current_network_bid_rub is None
    assert kw.quality_score is None


# --------------------------------------------------------------------------
# CampaignId field — needed so KeywordSnapshot.campaign_id can be
# populated without a second adgroup-lookup round trip.
# --------------------------------------------------------------------------


def test_keyword_parses_campaign_id_alias() -> None:
    kw = Keyword.model_validate(
        {
            "Id": 1,
            "AdGroupId": 100,
            "CampaignId": 7,
            "Keyword": "k",
        }
    )
    assert kw.campaign_id == 7


# --------------------------------------------------------------------------
# Bid + ContextBid: stored as micro-RUB, exposed as RUB.
# --------------------------------------------------------------------------


def test_keyword_exposes_search_bid_in_rubles() -> None:
    """Direct returns ``Bid`` in micro-currency (RUB * 1_000_000).
    KS#2 / KS#4 work in RUB. The model converts at the boundary so
    the service layer never sees micro units."""
    kw = Keyword.model_validate(
        {
            "Id": 1,
            "AdGroupId": 100,
            "Keyword": "k",
            "Bid": 12_500_000,  # 12.5 RUB
        }
    )
    assert kw.bid_micro == 12_500_000
    assert kw.current_search_bid_rub == 12.5


def test_keyword_exposes_network_bid_in_rubles() -> None:
    kw = Keyword.model_validate(
        {
            "Id": 1,
            "AdGroupId": 100,
            "Keyword": "k",
            "ContextBid": 3_000_000,  # 3 RUB
        }
    )
    assert kw.context_bid_micro == 3_000_000
    assert kw.current_network_bid_rub == 3.0


def test_keyword_zero_bid_distinct_from_missing_bid() -> None:
    """Zero is a real value (campaign-bid-not-overridden in some
    Direct configurations) and must NOT collapse to None — KS#4
    treats None as "unknown, defer" and 0 as "current bid is zero,
    any positive new value is an increase".
    """
    kw = Keyword.model_validate({"Id": 1, "AdGroupId": 100, "Keyword": "k", "Bid": 0})
    assert kw.bid_micro == 0
    assert kw.current_search_bid_rub == 0.0


@pytest.mark.parametrize("field_name", ["Bid", "ContextBid"])
def test_keyword_rejects_negative_bid(field_name: str) -> None:
    """Defensive: a negative ``Bid`` / ``ContextBid`` from a malformed
    or adversarially-shaped API row would land in the safety
    snapshot as a negative ``current_*_bid_rub`` and poison KS#4's
    ``_is_increase(new, current)`` comparison — a positive new bid
    against a negative baseline reads as an increase even when the
    operator was lowering the bid, blocking legitimate decreases.
    Equally, KS#2 cap arithmetic loses meaning. Reject at the model
    boundary so the failure surfaces as a ValidationError on
    ``get_keywords`` (loud, attributable) rather than a silent
    safety bypass (auditor M2-bid-snapshot HIGH-1).
    """
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Keyword.model_validate({"Id": 1, "AdGroupId": 100, "Keyword": "k", field_name: -1})


# --------------------------------------------------------------------------
# Productivity envelope → integer QS via property.
# --------------------------------------------------------------------------


def test_keyword_extracts_quality_score_from_productivity() -> None:
    """Direct returns ``Productivity`` as ``{"Value": float,
    "Recommendations": [...]}``. KS#4's ``KeywordSnapshot.quality_score``
    is ``int 0..10``; we round and clamp at the model boundary so the
    service layer doesn't have to.
    """
    kw = Keyword.model_validate(
        {
            "Id": 1,
            "AdGroupId": 100,
            "Keyword": "k",
            "Productivity": {"Value": 8.0, "Recommendations": []},
        }
    )
    assert kw.quality_score == 8


def test_keyword_quality_score_rounds_fractional_value() -> None:
    """Direct's Productivity.Value is documented as float in 0..10;
    KS#4 takes int. We round half UP (the operator's intuitive
    direction; matches Direct's own UI rendering of Productivity)
    rather than relying on Python's banker's rounding, which would
    silently flip the threshold verdict at exact .5 boundaries
    (auditor M2-bid-snapshot MEDIUM)."""
    kw = Keyword.model_validate(
        {
            "Id": 1,
            "AdGroupId": 100,
            "Keyword": "k",
            "Productivity": {"Value": 4.6},
        }
    )
    assert kw.quality_score == 5


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # Half-up rule pinned at every integer boundary that intersects
        # a plausible ``min_quality_score_for_bid_increase`` threshold.
        # A future refactor that swaps in banker's rounding (round-half-
        # to-even) would silently flip several of these — auditor
        # MEDIUM finding on this PR.
        (0.5, 1),
        (1.5, 2),
        (4.5, 5),
        (5.5, 6),
        (9.5, 10),
    ],
)
def test_keyword_quality_score_rounds_half_up_at_integer_boundaries(
    value: float, expected: int
) -> None:
    kw = Keyword.model_validate(
        {
            "Id": 1,
            "AdGroupId": 100,
            "Keyword": "k",
            "Productivity": {"Value": value},
        }
    )
    assert kw.quality_score == expected


def test_keyword_quality_score_none_when_productivity_missing() -> None:
    kw = Keyword.model_validate({"Id": 1, "AdGroupId": 100, "Keyword": "k"})
    assert kw.quality_score is None


def test_keyword_quality_score_none_when_value_missing() -> None:
    """Productivity envelope without a Value (rare; possible during
    Direct's "newly added, not yet scored" window) → unknown QS, not
    a hard zero. KS#4 treats None as defer (the right answer)."""
    kw = Keyword.model_validate(
        {
            "Id": 1,
            "AdGroupId": 100,
            "Keyword": "k",
            "Productivity": {"Recommendations": []},
        }
    )
    assert kw.quality_score is None


@pytest.mark.parametrize("bad_value", [-1, 11, -0.5, 10.5])
def test_keyword_quality_score_clamps_out_of_range_to_none(bad_value: float) -> None:
    """If Direct ever returns a value outside 0..10 (API change /
    sandbox quirk), surface as ``None`` rather than handing KS#4
    ``KeywordSnapshot.quality_score=11`` which would blow its
    ``__post_init__`` validator. Treat unexpected values as
    "unknown, defer" — same fail-open contract as a missing value.
    """
    kw = Keyword.model_validate(
        {
            "Id": 1,
            "AdGroupId": 100,
            "Keyword": "k",
            "Productivity": {"Value": bad_value},
        }
    )
    assert kw.quality_score is None
