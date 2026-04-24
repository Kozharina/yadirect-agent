"""Tests for ToolRegistry + the seven default tools.

Strategy:
- Registry mechanics (add/get/schemas/dup) are pure and tested directly.
- Per-tool handlers are exercised through a registry built against the test
  `settings` fixture. Services underneath are monkeypatched to return
  fixed shapes; no HTTP.
- Input validation: we rely on pydantic, so we spot-check the handful of
  non-trivial constraints (min_length, ge=300) rather than restating schema.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from yadirect_agent.agent.tools import (
    Tool,
    ToolContext,
    ToolRegistry,
    build_default_registry,
)
from yadirect_agent.config import Settings
from yadirect_agent.models.campaigns import (
    Campaign,
    CampaignState,
    CampaignStatus,
    DailyBudget,
)
from yadirect_agent.models.keywords import Keyword

# --------------------------------------------------------------------------
# Registry mechanics.
# --------------------------------------------------------------------------


class _EmptyInput(BaseModel):
    pass


def _noop_tool(name: str = "noop", *, is_write: bool = False) -> Tool:
    async def handler(_inp: BaseModel, _ctx: ToolContext) -> Any:
        return {"ok": True}

    return Tool(
        name=name,
        description="noop",
        input_model=_EmptyInput,
        is_write=is_write,
        handler=handler,
    )


class TestRegistry:
    def test_add_and_get_roundtrip(self) -> None:
        reg = ToolRegistry()
        reg.add(_noop_tool("alpha"))

        assert "alpha" in reg
        assert reg.get("alpha").name == "alpha"
        assert reg.names() == ["alpha"]
        assert len(reg) == 1

    def test_duplicate_add_is_rejected(self) -> None:
        reg = ToolRegistry()
        reg.add(_noop_tool("alpha"))
        with pytest.raises(ValueError, match="already registered"):
            reg.add(_noop_tool("alpha"))

    def test_get_missing_raises(self) -> None:
        reg = ToolRegistry()
        with pytest.raises(KeyError, match="unknown tool"):
            reg.get("missing")

    def test_schemas_match_anthropic_shape(self) -> None:
        reg = ToolRegistry()
        reg.add(_noop_tool("alpha"))
        schemas = reg.schemas()

        assert len(schemas) == 1
        s = schemas[0]
        assert set(s) == {"name", "description", "input_schema"}
        assert s["name"] == "alpha"
        # input_schema must be a JSON-schema-ish mapping.
        assert isinstance(s["input_schema"], dict)
        assert s["input_schema"].get("type") == "object"


# --------------------------------------------------------------------------
# build_default_registry: shape of the seven-tool set.
# --------------------------------------------------------------------------


class TestDefaultRegistry:
    def test_exposes_seven_named_tools(self, settings: Settings) -> None:
        reg = build_default_registry(settings)

        assert len(reg) == 7
        assert set(reg.names()) == {
            "list_campaigns",
            "pause_campaigns",
            "resume_campaigns",
            "set_campaign_budget",
            "get_keywords",
            "set_keyword_bids",
            "validate_phrases",
        }

    @pytest.mark.parametrize(
        ("name", "is_write"),
        [
            ("list_campaigns", False),
            ("pause_campaigns", True),
            ("resume_campaigns", True),
            ("set_campaign_budget", True),
            ("get_keywords", False),
            ("set_keyword_bids", True),
            ("validate_phrases", False),
        ],
    )
    def test_write_flags_match_spec(self, settings: Settings, name: str, is_write: bool) -> None:
        reg = build_default_registry(settings)
        assert reg.get(name).is_write is is_write


# --------------------------------------------------------------------------
# Per-tool handlers — dispatched against monkeypatched services.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_campaigns_default_returns_all(
    settings: Settings,
    tool_context: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from yadirect_agent.services.campaigns import CampaignService

    async def fake_list_all(self: CampaignService, _limit: int = 500) -> list:
        return []

    async def fake_list_active(self: CampaignService, limit: int = 200) -> list:
        raise AssertionError("should not be called when states=None")

    monkeypatch.setattr(CampaignService, "list_all", fake_list_all)
    monkeypatch.setattr(CampaignService, "list_active", fake_list_active)

    tool = build_default_registry(settings).get("list_campaigns")
    result = await tool.handler(tool.input_model(), tool_context)

    assert result == []


@pytest.mark.asyncio
async def test_list_campaigns_with_states_uses_list_active(
    settings: Settings,
    tool_context: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from yadirect_agent.services.campaigns import CampaignService

    async def fake_list_active(self: CampaignService, limit: int = 200) -> list:
        return []

    monkeypatch.setattr(CampaignService, "list_active", fake_list_active)

    tool = build_default_registry(settings).get("list_campaigns")
    inp = tool.input_model.model_validate({"states": ["ON"]})
    result = await tool.handler(inp, tool_context)

    assert result == []


@pytest.mark.asyncio
async def test_pause_campaigns_delegates_and_echoes_ids(
    settings: Settings,
    tool_context: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from yadirect_agent.services.campaigns import CampaignService

    captured: list[list[int]] = []

    async def fake_pause(self: CampaignService, ids: list[int]) -> None:
        captured.append(list(ids))

    monkeypatch.setattr(CampaignService, "pause", fake_pause)

    tool = build_default_registry(settings).get("pause_campaigns")
    inp = tool.input_model.model_validate({"ids": [1, 2]})
    result = await tool.handler(inp, tool_context)

    assert captured == [[1, 2]]
    assert result == {"paused": [1, 2]}


@pytest.mark.asyncio
async def test_resume_campaigns_delegates_and_echoes_ids(
    settings: Settings,
    tool_context: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from yadirect_agent.services.campaigns import CampaignService

    async def fake_resume(self: CampaignService, ids: list[int]) -> None:
        return None

    monkeypatch.setattr(CampaignService, "resume", fake_resume)

    tool = build_default_registry(settings).get("resume_campaigns")
    inp = tool.input_model.model_validate({"ids": [7]})
    result = await tool.handler(inp, tool_context)

    assert result == {"resumed": [7]}


def test_set_campaign_budget_rejects_below_minimum(settings: Settings) -> None:
    tool = build_default_registry(settings).get("set_campaign_budget")
    with pytest.raises(ValidationError):
        tool.input_model.model_validate({"campaign_id": 1, "budget_rub": 299})


@pytest.mark.asyncio
async def test_set_campaign_budget_passes_through(
    settings: Settings,
    tool_context: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from yadirect_agent.services.campaigns import CampaignService

    captured: list[tuple[int, int]] = []

    async def fake_set_budget(self: CampaignService, campaign_id: int, budget_rub: int) -> None:
        captured.append((campaign_id, budget_rub))

    monkeypatch.setattr(CampaignService, "set_daily_budget", fake_set_budget)

    tool = build_default_registry(settings).get("set_campaign_budget")
    inp = tool.input_model.model_validate({"campaign_id": 42, "budget_rub": 500})
    result = await tool.handler(inp, tool_context)

    assert captured == [(42, 500)]
    assert result == {"campaign_id": 42, "budget_rub": 500}


@pytest.mark.asyncio
async def test_get_keywords_returns_model_dumps(
    settings: Settings,
    tool_context: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from yadirect_agent.clients.direct import DirectService

    async def fake_aenter(self: DirectService) -> DirectService:
        return self

    async def fake_aexit(self: DirectService, *exc_info: object) -> None:
        return None

    async def fake_get_keywords(
        self: DirectService, adgroup_ids: list[int], limit: int = 10_000
    ) -> list[Keyword]:
        return [Keyword(Id=1, AdGroupId=10, Keyword="купить обувь", State="ON", Status="ACCEPTED")]

    monkeypatch.setattr(DirectService, "__aenter__", fake_aenter)
    monkeypatch.setattr(DirectService, "__aexit__", fake_aexit)
    monkeypatch.setattr(DirectService, "get_keywords", fake_get_keywords)

    tool = build_default_registry(settings).get("get_keywords")
    inp = tool.input_model.model_validate({"adgroup_ids": [10]})
    result = await tool.handler(inp, tool_context)

    assert isinstance(result, list)
    assert result[0]["keyword"] == "купить обувь"
    assert result[0]["id"] == 1


def test_get_keywords_requires_nonempty_adgroup_ids(settings: Settings) -> None:
    tool = build_default_registry(settings).get("get_keywords")
    with pytest.raises(ValidationError):
        tool.input_model.model_validate({"adgroup_ids": []})


@pytest.mark.asyncio
async def test_set_keyword_bids_converts_and_forwards(
    settings: Settings,
    tool_context: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from yadirect_agent.services.bidding import BiddingService, BidUpdate

    captured: list[list[BidUpdate]] = []

    async def fake_apply(self: BiddingService, updates: list[BidUpdate]) -> None:
        captured.append(list(updates))

    monkeypatch.setattr(BiddingService, "apply", fake_apply)

    tool = build_default_registry(settings).get("set_keyword_bids")
    inp = tool.input_model.model_validate(
        {"updates": [{"keyword_id": 1, "new_search_bid_rub": 10.0}]}
    )
    result = await tool.handler(inp, tool_context)

    assert len(captured) == 1
    assert captured[0][0].keyword_id == 1
    assert captured[0][0].new_search_bid_rub == 10.0
    assert result == {"updated": [1]}


@pytest.mark.asyncio
async def test_validate_phrases_maps_presence(
    settings: Settings,
    tool_context: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from yadirect_agent.clients.wordstat import DirectKeywordsResearch

    async def fake_has_search_volume(
        self: DirectKeywordsResearch,
        phrases: list[str],
        geo: list[int] | None = None,
    ) -> dict[str, bool]:
        return {p: i % 2 == 0 for i, p in enumerate(phrases)}

    monkeypatch.setattr(DirectKeywordsResearch, "has_search_volume", fake_has_search_volume)

    tool = build_default_registry(settings).get("validate_phrases")
    inp = tool.input_model.model_validate({"phrases": ["a", "b", "c"]})
    result = await tool.handler(inp, tool_context)

    assert result == {"a": True, "b": False, "c": True}


def test_validate_phrases_requires_nonempty_phrases(settings: Settings) -> None:
    tool = build_default_registry(settings).get("validate_phrases")
    with pytest.raises(ValidationError):
        tool.input_model.model_validate({"phrases": []})


# --------------------------------------------------------------------------
# list_campaigns output shape sanity-check — services may evolve, but the
# tool contract stays the same (flat dict of CampaignSummary fields).
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_campaigns_summary_shape(
    settings: Settings,
    tool_context: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from yadirect_agent.services.campaigns import CampaignService

    # The service wraps `Campaign` in `CampaignSummary`. The tool converts
    # that summary to a dict. We bypass `DirectService` by patching the
    # service method to produce summaries directly.
    from yadirect_agent.services.campaigns import CampaignSummary

    async def fake_summaries(self: CampaignService, _limit: int = 500) -> list[CampaignSummary]:
        return [
            CampaignSummary(
                id=1,
                name="alpha",
                state="ON",
                status="ACCEPTED",
                type="TEXT_CAMPAIGN",
                daily_budget_rub=500.0,
            )
        ]

    monkeypatch.setattr(CampaignService, "list_all", fake_summaries)

    tool = build_default_registry(settings).get("list_campaigns")
    result = await tool.handler(tool.input_model(), tool_context)

    assert result == [
        {
            "id": 1,
            "name": "alpha",
            "state": "ON",
            "status": "ACCEPTED",
            "type": "TEXT_CAMPAIGN",
            "daily_budget_rub": 500.0,
        }
    ]
