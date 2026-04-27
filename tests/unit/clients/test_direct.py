"""Tests for ``DirectService.get_keywords``.

Covers the safety-snapshot reader contract added for KS#2 / KS#4:

- request shape — selection by adgroup_ids OR keyword_ids; FieldNames
  always include the bid + productivity fields the safety snapshot
  needs;
- input validation — at least one selection must be supplied;
- response parsing — bid + Productivity envelope reaches the
  ``Keyword`` model unchanged.

Why the keyword_ids selection: ``BiddingService._build_bid_context``
is called with a list of ``BidUpdate(keyword_id=...)`` and has no
adgroup context — fetching by keyword_ids avoids a second
adgroup-lookup round trip just to populate the safety snapshot.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from yadirect_agent.clients.direct import DirectService
from yadirect_agent.config import Settings

_SAFETY_FIELD_NAMES = {
    "Id",
    "AdGroupId",
    "CampaignId",
    "Keyword",
    "State",
    "Status",
    "Bid",
    "ContextBid",
    "Productivity",
}


# --------------------------------------------------------------------------
# Request shape — selection + FieldNames.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_keywords_by_adgroup_ids_requests_safety_fields(
    settings: Settings, respx_mock: respx.MockRouter
) -> None:
    """The legacy adgroup-based call site keeps working AND now
    requests the bid + productivity fields the safety pipeline
    needs. Existing callers (agent tools' ``get_keywords``) get the
    new fields for free without any caller-side change.
    """
    route = respx_mock.post("https://api-sandbox.direct.yandex.com/json/v5/keywords").mock(
        return_value=httpx.Response(200, json={"result": {"Keywords": []}})
    )

    async with DirectService(settings) as svc:
        await svc.get_keywords(adgroup_ids=[1, 2])

    body = json.loads(route.calls[0].request.content.decode())
    assert body["method"] == "get"
    assert body["params"]["SelectionCriteria"] == {"AdGroupIds": [1, 2]}
    # Order isn't part of the contract, but the set is.
    assert set(body["params"]["FieldNames"]) >= _SAFETY_FIELD_NAMES


@pytest.mark.asyncio
async def test_get_keywords_by_keyword_ids_uses_ids_selection(
    settings: Settings, respx_mock: respx.MockRouter
) -> None:
    """``BiddingService._build_bid_context`` calls this path: a flat
    list of keyword_ids → SelectionCriteria.Ids on the wire."""
    route = respx_mock.post("https://api-sandbox.direct.yandex.com/json/v5/keywords").mock(
        return_value=httpx.Response(200, json={"result": {"Keywords": []}})
    )

    async with DirectService(settings) as svc:
        await svc.get_keywords(keyword_ids=[42, 99])

    body = json.loads(route.calls[0].request.content.decode())
    assert body["params"]["SelectionCriteria"] == {"Ids": [42, 99]}
    assert set(body["params"]["FieldNames"]) >= _SAFETY_FIELD_NAMES


@pytest.mark.asyncio
async def test_get_keywords_supports_combined_selection(
    settings: Settings, respx_mock: respx.MockRouter
) -> None:
    """Direct accepts both selectors as an AND filter — keep the
    behaviour available even though no current call site uses it.
    Pinned so an over-eager refactor that flips one selector to
    "exclusive or" doesn't sneak past review."""
    route = respx_mock.post("https://api-sandbox.direct.yandex.com/json/v5/keywords").mock(
        return_value=httpx.Response(200, json={"result": {"Keywords": []}})
    )

    async with DirectService(settings) as svc:
        await svc.get_keywords(adgroup_ids=[1], keyword_ids=[42])

    body = json.loads(route.calls[0].request.content.decode())
    assert body["params"]["SelectionCriteria"] == {"AdGroupIds": [1], "Ids": [42]}


@pytest.mark.asyncio
async def test_get_keywords_requires_at_least_one_selection(settings: Settings) -> None:
    """No adgroup_ids and no keyword_ids → ValueError before any
    network call. A wide-open ``keywords.get`` with empty selection
    would either error on Yandex's side or (worse) return every
    keyword on the account; neither is what any caller wants."""
    async with DirectService(settings) as svc:
        with pytest.raises(ValueError, match=r"adgroup_ids|keyword_ids"):
            await svc.get_keywords()


# --------------------------------------------------------------------------
# Response parsing.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_keywords_parses_bid_and_productivity_into_model(
    settings: Settings, respx_mock: respx.MockRouter
) -> None:
    respx_mock.post("https://api-sandbox.direct.yandex.com/json/v5/keywords").mock(
        return_value=httpx.Response(
            200,
            json={
                "result": {
                    "Keywords": [
                        {
                            "Id": 42,
                            "AdGroupId": 100,
                            "CampaignId": 7,
                            "Keyword": "k",
                            "State": "ON",
                            "Status": "ACCEPTED",
                            "Bid": 12_500_000,
                            "ContextBid": 3_000_000,
                            "Productivity": {"Value": 8.0},
                        }
                    ]
                }
            },
        )
    )

    async with DirectService(settings) as svc:
        keywords = await svc.get_keywords(keyword_ids=[42])

    [kw] = keywords
    assert kw.id == 42
    assert kw.campaign_id == 7
    assert kw.current_search_bid_rub == 12.5
    assert kw.current_network_bid_rub == 3.0
    assert kw.quality_score == 8
