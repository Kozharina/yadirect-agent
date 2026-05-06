"""Tests for ``DirectService`` read methods.

Covers:

- ``get_campaigns`` — request shape includes the ``NegativeKeywords``
  field (KS#3) and parses the envelope into a flat list.
- ``get_keywords`` — selection by adgroup_ids OR keyword_ids;
  FieldNames always include the bid + productivity fields the
  safety snapshot needs (KS#2 / KS#4); input validation; response
  parsing of bid + Productivity into the ``Keyword`` model;
  optional ``statuses`` filter for the rejected-keyword scan.
- ``get_ads`` — minimal pin tests for the request shape, plus the
  optional ``statuses`` filter for the rejected-ad scan.
- ``scan_rejected_ads`` / ``scan_rejected_keywords`` — composed
  walkers (campaign_ids → adgroups → ads/keywords with server-side
  ``Statuses=[REJECTED]`` filter). Powers ``RejectedAdsRule`` and
  ``RejectedKeywordsRule`` in ``HealthCheckService`` (M15.5.2-3).

Why the keyword_ids selection on ``get_keywords``:
``BiddingService._build_bid_context`` is called with a list of
``BidUpdate(keyword_id=...)`` and has no adgroup context — fetching
by keyword_ids avoids a second adgroup-lookup round trip just to
populate the safety snapshot.

Why the server-side ``Statuses`` filter for the scan helpers:
healthy accounts have hundreds to thousands of ads / keywords;
fetching them all and filtering locally would burn bandwidth and
hit Direct's per-call row limits. ``SelectionCriteria.Statuses=
["REJECTED"]`` returns only the rows we care about — the same
data shape the API ergonomically supports.
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


_CAMPAIGN_SAFETY_FIELD_NAMES = {
    "Id",
    "Name",
    "State",
    "Status",
    "DailyBudget",
    "NegativeKeywords",
}


# --------------------------------------------------------------------------
# Campaigns: NegativeKeywords field added for KS#3 (negative-keyword floor).
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_campaigns_requests_negative_keywords_field(
    settings: Settings, respx_mock: respx.MockRouter
) -> None:
    """``_build_resume_context`` populates ``CampaignBudget.negative_keywords``
    from the snapshot's campaigns. For that pipe to carry real data,
    the wire request must opt into the ``NegativeKeywords`` FieldName.
    Without this, the snapshot is empty and KS#3 blocks every resume
    once an operator configures ``required_negative_keywords``."""
    route = respx_mock.post("https://api-sandbox.direct.yandex.com/json/v5/campaigns").mock(
        return_value=httpx.Response(200, json={"result": {"Campaigns": []}})
    )

    async with DirectService(settings) as svc:
        await svc.get_campaigns()

    body = json.loads(route.calls[0].request.content.decode())
    assert set(body["params"]["FieldNames"]) >= _CAMPAIGN_SAFETY_FIELD_NAMES


@pytest.mark.asyncio
async def test_get_campaigns_parses_negative_keywords_envelope(
    settings: Settings, respx_mock: respx.MockRouter
) -> None:
    """End-to-end of the wire-to-model pipe for KS#3: a campaign row
    carrying the ``NegativeKeywords`` envelope flows through to a
    flat ``Campaign.negative_keywords`` list."""
    respx_mock.post("https://api-sandbox.direct.yandex.com/json/v5/campaigns").mock(
        return_value=httpx.Response(
            200,
            json={
                "result": {
                    "Campaigns": [
                        {
                            "Id": 7,
                            "Name": "c1",
                            "State": "ON",
                            "Status": "ACCEPTED",
                            "NegativeKeywords": {"Items": ["бесплатно", "отзывы"]},
                        }
                    ]
                }
            },
        )
    )

    async with DirectService(settings) as svc:
        campaigns = await svc.get_campaigns()

    [c] = campaigns
    assert c.id == 7
    assert c.negative_keywords == ["бесплатно", "отзывы"]


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


# --------------------------------------------------------------------------
# get_keywords — optional ``statuses`` filter (M15.5.2 rejected-keywords).
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_keywords_with_statuses_adds_to_selection_criteria(
    settings: Settings, respx_mock: respx.MockRouter
) -> None:
    # ``RejectedKeywordsRule`` calls this with ``statuses=["REJECTED"]``
    # so Direct returns only moderation-rejected rows. Server-side
    # filtering matters because a healthy account has thousands of
    # keywords and the full list would burn bandwidth + hit Direct's
    # per-call row limits.
    route = respx_mock.post("https://api-sandbox.direct.yandex.com/json/v5/keywords").mock(
        return_value=httpx.Response(200, json={"result": {"Keywords": []}})
    )

    async with DirectService(settings) as svc:
        await svc.get_keywords(adgroup_ids=[1, 2], statuses=["REJECTED"])

    body = json.loads(route.calls[0].request.content.decode())
    assert body["params"]["SelectionCriteria"]["Statuses"] == ["REJECTED"]
    # AdGroupIds must still be present — Statuses is an AND-filter,
    # not a replacement.
    assert body["params"]["SelectionCriteria"]["AdGroupIds"] == [1, 2]


@pytest.mark.asyncio
async def test_get_keywords_without_statuses_omits_field(
    settings: Settings, respx_mock: respx.MockRouter
) -> None:
    # Backward compatibility: existing callers pass no ``statuses``;
    # the wire request must NOT include a ``Statuses`` key (Direct
    # rejects empty arrays in some paths). Default-None preserves
    # the contract every existing test pins.
    route = respx_mock.post("https://api-sandbox.direct.yandex.com/json/v5/keywords").mock(
        return_value=httpx.Response(200, json={"result": {"Keywords": []}})
    )

    async with DirectService(settings) as svc:
        await svc.get_keywords(adgroup_ids=[1])

    body = json.loads(route.calls[0].request.content.decode())
    assert "Statuses" not in body["params"]["SelectionCriteria"]


# --------------------------------------------------------------------------
# get_ads — minimal pin tests + optional ``statuses`` filter.
# --------------------------------------------------------------------------
# Coverage debt acknowledged in BACKLOG ("methods with no respx tests");
# folding the minimum into this PR because ``scan_rejected_ads`` is the
# first service path actually using ``get_ads``. Full coverage stays a
# separate follow-up.


@pytest.mark.asyncio
async def test_get_ads_requests_adgroup_ids_selection(
    settings: Settings, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.post("https://api-sandbox.direct.yandex.com/json/v5/ads").mock(
        return_value=httpx.Response(200, json={"result": {"Ads": []}})
    )

    async with DirectService(settings) as svc:
        await svc.get_ads(adgroup_ids=[10, 20])

    body = json.loads(route.calls[0].request.content.decode())
    assert body["method"] == "get"
    assert body["params"]["SelectionCriteria"]["AdGroupIds"] == [10, 20]
    # Ads.get FieldNames must include Status + State so the rejected
    # scan sees moderation status; TextAdFieldNames must include
    # Title so the operator-facing finding can quote which ad text
    # got rejected (helps triage faster than just an ad_id).
    assert "Status" in body["params"]["FieldNames"]
    assert "State" in body["params"]["FieldNames"]
    assert "Title" in body["params"]["TextAdFieldNames"]


@pytest.mark.asyncio
async def test_get_ads_with_statuses_adds_to_selection_criteria(
    settings: Settings, respx_mock: respx.MockRouter
) -> None:
    # ``RejectedAdsRule`` calls this with ``statuses=["REJECTED"]``
    # for the same reason ``RejectedKeywordsRule`` does — let the
    # server return only the rows we care about.
    route = respx_mock.post("https://api-sandbox.direct.yandex.com/json/v5/ads").mock(
        return_value=httpx.Response(200, json={"result": {"Ads": []}})
    )

    async with DirectService(settings) as svc:
        await svc.get_ads(adgroup_ids=[10], statuses=["REJECTED"])

    body = json.loads(route.calls[0].request.content.decode())
    assert body["params"]["SelectionCriteria"]["Statuses"] == ["REJECTED"]
    assert body["params"]["SelectionCriteria"]["AdGroupIds"] == [10]


@pytest.mark.asyncio
async def test_get_ads_without_statuses_omits_field(
    settings: Settings, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.post("https://api-sandbox.direct.yandex.com/json/v5/ads").mock(
        return_value=httpx.Response(200, json={"result": {"Ads": []}})
    )

    async with DirectService(settings) as svc:
        await svc.get_ads(adgroup_ids=[10])

    body = json.loads(route.calls[0].request.content.decode())
    assert "Statuses" not in body["params"]["SelectionCriteria"]


# --------------------------------------------------------------------------
# scan_rejected_ads — composed walker for RejectedAdsRule.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scan_rejected_ads_walks_campaigns_to_adgroups_to_ads(
    settings: Settings, respx_mock: respx.MockRouter
) -> None:
    # End-to-end of the two-step walk: first an adgroups.get to
    # resolve campaign_ids → adgroup_ids, then ads.get with
    # Statuses=[REJECTED] to fetch only moderation-rejected rows.
    # The ad-group lookup is unavoidable: Direct's ads.get accepts
    # AdGroupIds but not CampaignIds, so we have to flatten the
    # campaign → group → ad hierarchy ourselves.
    adgroups_route = respx_mock.post("https://api-sandbox.direct.yandex.com/json/v5/adgroups").mock(
        return_value=httpx.Response(
            200,
            json={
                "result": {
                    "AdGroups": [
                        {"Id": 100, "Name": "g1", "CampaignId": 7},
                        {"Id": 200, "Name": "g2", "CampaignId": 7},
                    ]
                }
            },
        )
    )
    ads_route = respx_mock.post("https://api-sandbox.direct.yandex.com/json/v5/ads").mock(
        return_value=httpx.Response(
            200,
            json={
                "result": {
                    "Ads": [
                        {
                            "Id": 5001,
                            "AdGroupId": 100,
                            "CampaignId": 7,
                            "Status": "REJECTED",
                            "State": "ON",
                            "TextAd": {"Title": "headline"},
                        }
                    ]
                }
            },
        )
    )

    async with DirectService(settings) as svc:
        rejected = await svc.scan_rejected_ads(campaign_ids=[7])

    assert adgroups_route.called
    assert ads_route.called
    # Adgroups call uses CampaignIds selection.
    adg_body = json.loads(adgroups_route.calls[0].request.content.decode())
    assert adg_body["params"]["SelectionCriteria"]["CampaignIds"] == [7]
    # Ads call uses the resolved AdGroupIds + Statuses=[REJECTED].
    ads_body = json.loads(ads_route.calls[0].request.content.decode())
    assert sorted(ads_body["params"]["SelectionCriteria"]["AdGroupIds"]) == [100, 200]
    assert ads_body["params"]["SelectionCriteria"]["Statuses"] == ["REJECTED"]
    # Single rejected ad bubbles up unchanged for the rule layer to
    # render. Caller (rule) decides how to format the operator-facing
    # message; the client stays a thin facade.
    assert len(rejected) == 1
    assert rejected[0]["Id"] == 5001


@pytest.mark.asyncio
async def test_scan_rejected_ads_returns_empty_for_empty_campaign_ids(
    settings: Settings,
) -> None:
    # Early return: no campaigns means no work. Crucially, NO HTTP
    # call — opening a respx mock here would fail-not-mocked, so
    # the absence of a respx_mock fixture is itself the assertion
    # that the method short-circuits.
    async with DirectService(settings) as svc:
        result = await svc.scan_rejected_ads(campaign_ids=[])

    assert result == []


@pytest.mark.asyncio
async def test_scan_rejected_ads_returns_empty_when_no_adgroups(
    settings: Settings, respx_mock: respx.MockRouter
) -> None:
    # Campaign exists but has no ad-groups (fresh campaign, or one
    # whose only group was archived). Skip the second hop instead
    # of calling ads.get with an empty AdGroupIds list — Direct
    # rejects empty selectors with a confusing error and the rule
    # would crash mid-check.
    respx_mock.post("https://api-sandbox.direct.yandex.com/json/v5/adgroups").mock(
        return_value=httpx.Response(200, json={"result": {"AdGroups": []}})
    )
    # If the implementation ignores the empty-adgroups guard and
    # calls ads.get anyway, respx will fail-not-mocked here.

    async with DirectService(settings) as svc:
        result = await svc.scan_rejected_ads(campaign_ids=[7])

    assert result == []


# --------------------------------------------------------------------------
# scan_rejected_keywords — composed walker for RejectedKeywordsRule.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scan_rejected_keywords_walks_campaigns_to_adgroups_to_keywords(
    settings: Settings, respx_mock: respx.MockRouter
) -> None:
    # Symmetric to scan_rejected_ads but ends with keywords.get +
    # Statuses=[REJECTED]. Returns ``Keyword`` model instances (not
    # dicts) because keywords.get already routes through the typed
    # model — RejectedKeywordsRule benefits from the Pydantic field
    # access.
    respx_mock.post("https://api-sandbox.direct.yandex.com/json/v5/adgroups").mock(
        return_value=httpx.Response(
            200,
            json={
                "result": {
                    "AdGroups": [
                        {"Id": 100, "Name": "g1", "CampaignId": 7},
                    ]
                }
            },
        )
    )
    keywords_route = respx_mock.post("https://api-sandbox.direct.yandex.com/json/v5/keywords").mock(
        return_value=httpx.Response(
            200,
            json={
                "result": {
                    "Keywords": [
                        {
                            "Id": 999,
                            "AdGroupId": 100,
                            "CampaignId": 7,
                            "Keyword": "купить слона",
                            "State": "ON",
                            "Status": "REJECTED",
                        }
                    ]
                }
            },
        )
    )

    async with DirectService(settings) as svc:
        rejected = await svc.scan_rejected_keywords(campaign_ids=[7])

    kw_body = json.loads(keywords_route.calls[0].request.content.decode())
    assert kw_body["params"]["SelectionCriteria"]["AdGroupIds"] == [100]
    assert kw_body["params"]["SelectionCriteria"]["Statuses"] == ["REJECTED"]
    assert len(rejected) == 1
    assert rejected[0].id == 999
    assert rejected[0].keyword == "купить слона"
    assert rejected[0].status == "REJECTED"


@pytest.mark.asyncio
async def test_scan_rejected_keywords_returns_empty_for_empty_campaign_ids(
    settings: Settings,
) -> None:
    async with DirectService(settings) as svc:
        result = await svc.scan_rejected_keywords(campaign_ids=[])

    assert result == []


@pytest.mark.asyncio
async def test_scan_rejected_keywords_returns_empty_when_no_adgroups(
    settings: Settings, respx_mock: respx.MockRouter
) -> None:
    respx_mock.post("https://api-sandbox.direct.yandex.com/json/v5/adgroups").mock(
        return_value=httpx.Response(200, json={"result": {"AdGroups": []}})
    )

    async with DirectService(settings) as svc:
        result = await svc.scan_rejected_keywords(campaign_ids=[7])

    assert result == []
