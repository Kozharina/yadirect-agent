"""High-level Yandex Direct API client.

Wraps the raw JSON-RPC calls with typed methods and Pydantic models.
Each method maps 1:1 to a Direct API method — no business logic here.
Business logic lives in services/.
"""

from __future__ import annotations

import asyncio
from typing import Any, Self

import httpx

from ..config import Settings
from ..exceptions import ApiTransientError
from ..models.campaigns import Campaign
from ..models.keywords import Keyword, KeywordBid
from .base import DirectApiClient


class DirectService:
    """Ergonomic facade on top of DirectApiClient."""

    def __init__(self, settings: Settings) -> None:
        self._api = DirectApiClient(settings)
        self._settings = settings

    async def __aenter__(self) -> Self:
        await self._api.__aenter__()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self._api.__aexit__(*exc_info)

    # ---------------- Campaigns ----------------

    async def get_campaigns(
        self,
        ids: list[int] | None = None,
        states: list[str] | None = None,
        types: list[str] | None = None,
        limit: int = 500,
    ) -> list[Campaign]:
        selection: dict[str, Any] = {}
        if ids:
            selection["Ids"] = ids
        if states:
            selection["States"] = states
        if types:
            selection["Types"] = types

        field_names = [
            "Id",
            "Name",
            "State",
            "Status",
            "Type",
            "StartDate",
            "EndDate",
            "DailyBudget",
            "ClientInfo",
            # Per-campaign negatives are needed for KS#3 to compare
            # ``CampaignBudget.negative_keywords`` against
            # ``Policy.required_negative_keywords`` at resume time.
            # Direct returns ``{"Items": [...]}``; the Campaign model
            # flattens at validation time.
            "NegativeKeywords",
        ]

        result = await self._api.call(
            "campaigns",
            "get",
            {
                "SelectionCriteria": selection,
                "FieldNames": field_names,
                "Page": {"Limit": limit},
            },
        )
        return [Campaign.model_validate(c) for c in result.get("Campaigns", [])]

    async def suspend_campaigns(self, ids: list[int]) -> dict[str, Any]:
        return await self._api.call("campaigns", "suspend", {"SelectionCriteria": {"Ids": ids}})

    async def resume_campaigns(self, ids: list[int]) -> dict[str, Any]:
        return await self._api.call("campaigns", "resume", {"SelectionCriteria": {"Ids": ids}})

    async def archive_campaigns(self, ids: list[int]) -> dict[str, Any]:
        return await self._api.call("campaigns", "archive", {"SelectionCriteria": {"Ids": ids}})

    async def update_campaign_budget(
        self, campaign_id: int, daily_budget_rub: int, mode: str = "STANDARD"
    ) -> dict[str, Any]:
        return await self._api.call(
            "campaigns",
            "update",
            {
                "Campaigns": [
                    {
                        "Id": campaign_id,
                        "DailyBudget": {
                            "Amount": daily_budget_rub * 1_000_000,
                            "Mode": mode,
                        },
                    }
                ]
            },
        )

    # ---------------- Ad groups ----------------

    async def get_adgroups(
        self, campaign_ids: list[int], limit: int = 1000
    ) -> list[dict[str, Any]]:
        result = await self._api.call(
            "adgroups",
            "get",
            {
                "SelectionCriteria": {"CampaignIds": campaign_ids},
                "FieldNames": ["Id", "Name", "CampaignId", "Status", "Type"],
                "Page": {"Limit": limit},
            },
        )
        return list(result.get("AdGroups", []))

    # ---------------- Ads ----------------

    async def get_ads(
        self,
        adgroup_ids: list[int],
        *,
        statuses: list[str] | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """Read ad rows.

        ``statuses`` (e.g. ``["REJECTED"]``) is plumbed through as
        ``SelectionCriteria.Statuses`` so ``RejectedAdsRule`` can ask
        Direct for moderation-rejected ads only — server-side filtering
        keeps bandwidth + per-call row counts bounded on healthy
        accounts that can have thousands of approved ads. Default
        ``None`` preserves the existing wire shape for callers that
        want every ad regardless of moderation state.
        """
        selection: dict[str, Any] = {"AdGroupIds": adgroup_ids}
        if statuses:
            # Direct rejects empty arrays in some paths; only attach
            # the key when non-empty. ``if statuses`` covers both
            # ``None`` and ``[]``.
            selection["Statuses"] = statuses
        result = await self._api.call(
            "ads",
            "get",
            {
                "SelectionCriteria": selection,
                "FieldNames": ["Id", "AdGroupId", "CampaignId", "Status", "State", "Type"],
                "TextAdFieldNames": ["Title", "Title2", "Text", "Href", "DisplayUrlPath"],
                "Page": {"Limit": limit},
            },
        )
        return list(result.get("Ads", []))

    # ---------------- Keywords ----------------

    async def get_keywords(
        self,
        adgroup_ids: list[int] | None = None,
        *,
        keyword_ids: list[int] | None = None,
        statuses: list[str] | None = None,
        limit: int = 10_000,
    ) -> list[Keyword]:
        """Read keyword rows including the bid + productivity fields the
        safety pipeline needs for KS#2 / KS#4.

        Selection: pass ``adgroup_ids`` (legacy callers) or
        ``keyword_ids`` (BiddingService bid-context reader), or both
        for an AND filter. At least one is required — an unfiltered
        ``keywords.get`` would either error or return the whole
        account, neither of which any caller wants.

        ``statuses`` (e.g. ``["REJECTED"]``) is plumbed through as
        ``SelectionCriteria.Statuses`` so ``RejectedKeywordsRule`` can
        ask Direct for moderation-rejected keywords only. Same
        reasoning as ``get_ads``: server-side filter is the only sane
        approach on accounts with thousands of keywords.

        Returned ``Keyword`` rows expose ``current_search_bid_rub``,
        ``current_network_bid_rub`` and ``quality_score`` via computed
        properties; downstream code never needs to handle micro-currency
        or the Productivity envelope itself.
        """
        if not adgroup_ids and not keyword_ids:
            msg = "get_keywords requires adgroup_ids or keyword_ids"
            raise ValueError(msg)

        selection: dict[str, Any] = {}
        if adgroup_ids:
            selection["AdGroupIds"] = adgroup_ids
        if keyword_ids:
            selection["Ids"] = keyword_ids
        if statuses:
            selection["Statuses"] = statuses

        result = await self._api.call(
            "keywords",
            "get",
            {
                "SelectionCriteria": selection,
                "FieldNames": [
                    "Id",
                    "AdGroupId",
                    "CampaignId",
                    "Keyword",
                    "State",
                    "Status",
                    "Bid",
                    "ContextBid",
                    "Productivity",
                ],
                "Page": {"Limit": limit},
            },
        )
        return [Keyword.model_validate(k) for k in result.get("Keywords", [])]

    # ---------------- Rejected-entity scans (M15.5.2-3) ----------------

    async def scan_rejected_ads(self, campaign_ids: list[int]) -> list[dict[str, Any]]:
        """Find ads with ``Status=REJECTED`` across the given campaigns.

        Two-step walk: ``adgroups.get`` resolves campaign_ids →
        adgroup_ids, then ``ads.get`` filters server-side by
        ``Statuses=[REJECTED]``. The walk is unavoidable — Direct's
        ``ads.get`` accepts ``AdGroupIds`` but not ``CampaignIds``,
        so the campaign → group → ad hierarchy must be flattened
        client-side.

        Returns the raw ad-row dicts (not a typed model) because
        ``ads.get`` already returns dicts and the rule layer renders
        a free-form message — no need to invent a typed shape just
        to immediately stringify it.

        Empty ``campaign_ids`` short-circuits to ``[]`` (no work
        means no HTTP). Empty adgroups (campaign exists but has no
        ad-groups) also short-circuits — Direct rejects empty
        ``AdGroupIds`` selectors with a confusing error.
        """
        if not campaign_ids:
            return []
        adgroups = await self.get_adgroups(campaign_ids=campaign_ids)
        adgroup_ids = [int(g["Id"]) for g in adgroups if "Id" in g]
        if not adgroup_ids:
            return []
        return await self.get_ads(adgroup_ids=adgroup_ids, statuses=["REJECTED"])

    async def scan_rejected_keywords(self, campaign_ids: list[int]) -> list[Keyword]:
        """Find keywords with ``Status=REJECTED`` across the given campaigns.

        Symmetric to ``scan_rejected_ads`` but ends with
        ``keywords.get``. Returns ``Keyword`` model instances (not
        dicts) because ``get_keywords`` already routes through the
        typed model — ``RejectedKeywordsRule`` benefits from the
        Pydantic field access.
        """
        if not campaign_ids:
            return []
        adgroups = await self.get_adgroups(campaign_ids=campaign_ids)
        adgroup_ids = [int(g["Id"]) for g in adgroups if "Id" in g]
        if not adgroup_ids:
            return []
        return await self.get_keywords(adgroup_ids=adgroup_ids, statuses=["REJECTED"])

    async def add_keywords(self, keywords: list[dict[str, Any]]) -> dict[str, Any]:
        """keywords: list of {'AdGroupId': int, 'Keyword': str, 'Bid': int?}"""
        return await self._api.call("keywords", "add", {"Keywords": keywords})

    async def set_keyword_bids(self, bids: list[KeywordBid]) -> dict[str, Any]:
        return await self._api.call(
            "keywordbids",
            "set",
            {"KeywordBids": [b.model_dump(by_alias=True, exclude_none=True) for b in bids]},
        )

    # ---------------- Reports (async, TSV) ----------------

    async def fetch_report(
        self,
        report_body: dict[str, Any],
        *,
        poll_interval: float = 5.0,
        max_wait_seconds: float = 300.0,
    ) -> str:
        """Fetch a report via the async reports endpoint.

        The reports service is unusual:
        - Content-Type includes processing mode header.
        - Initial call returns HTTP 201/202 ('queued' / 'in progress').
        - We poll the same request until HTTP 200, then read the TSV body.

        Returns raw TSV text. Parsing is left to the reporting service.
        """
        url = f"{self._settings.direct_base_url}/reports"
        headers = {
            "Authorization": (f"Bearer {self._settings.yandex_direct_token.get_secret_value()}"),
            "Accept-Language": "ru",
            "processingMode": "auto",
            "returnMoneyInMicros": "false",
            "skipReportHeader": "true",
            "skipReportSummary": "true",
        }
        if self._settings.yandex_client_login:
            headers["Client-Login"] = self._settings.yandex_client_login

        deadline = asyncio.get_event_loop().time() + max_wait_seconds
        async with httpx.AsyncClient(timeout=60.0) as client:
            while True:
                r = await client.post(url, json=report_body, headers=headers)
                if r.status_code == 200:
                    return r.text
                if r.status_code in (201, 202):
                    retry_in = float(r.headers.get("retryIn", poll_interval))
                    if asyncio.get_event_loop().time() + retry_in > deadline:
                        raise ApiTransientError("report generation timed out")
                    await asyncio.sleep(retry_in)
                    continue
                raise ApiTransientError(f"report HTTP {r.status_code}: {r.text[:200]}")
