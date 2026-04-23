"""Tool registry and the seven core Yandex Direct tools.

Design choices
--------------
- A tool is a plain `Tool` dataclass — name, description, input pydantic model,
  write flag, and an async handler. We do not lean on a decorator global
  registry because the tool set is bound to a specific Settings instance
  (sandbox vs. prod, Client-Login, etc.). Global state would conflate
  configuration with code.
- `ToolRegistry` is an explicit object. Agents take one in their constructor.
  Tests substitute a registry with fakes, no monkeypatching.
- Descriptions are written **for the LLM**: purpose, when to use, what it
  returns, common failure modes. Keep them tight.
- `is_write` flags tools that mutate state. The agent loop uses this to
  enforce serial execution for writes while allowing parallel reads.
- Every handler returns a JSON-serialisable value. If a service yields
  dataclasses, we convert at the tool boundary — keeping Anthropic's wire
  format clean.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from typing import Any

import structlog
from pydantic import BaseModel, Field

from ..clients.direct import DirectService
from ..clients.wordstat import DirectKeywordsResearch
from ..config import Settings
from ..services.bidding import BiddingService, BidUpdate
from ..services.campaigns import CampaignService

# --------------------------------------------------------------------------
# Context + value types.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolContext:
    """Per-invocation context handed to every tool handler.

    trace_id ties all log lines from one agent turn together so a failure
    can be reconstructed end-to-end. The logger is already bound to the
    agent / trace_id — tools just add their own fields.
    """

    trace_id: str
    logger: structlog.stdlib.BoundLogger


ToolHandler = Callable[[BaseModel, ToolContext], Awaitable[Any]]


@dataclass(frozen=True)
class Tool:
    """Descriptor for a single agent-callable tool."""

    name: str
    description: str
    input_model: type[BaseModel]
    is_write: bool
    handler: ToolHandler

    def to_anthropic_schema(self) -> dict[str, Any]:
        """Shape expected by `anthropic.messages.create(tools=[...])`."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_model.model_json_schema(),
        }


# --------------------------------------------------------------------------
# Registry.
# --------------------------------------------------------------------------


class ToolRegistry:
    """Explicit, settings-bound collection of tools.

    Construct with `build_default_registry(settings)` for the standard set,
    or instantiate empty and `.add()` custom tools in tests.
    """

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def add(self, tool: Tool) -> None:
        if tool.name in self._tools:
            msg = f"tool already registered: {tool.name!r}"
            raise ValueError(msg)
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError as exc:
            msg = f"unknown tool: {name!r}"
            raise KeyError(msg) from exc

    def names(self) -> list[str]:
        return list(self._tools)

    def schemas(self) -> list[dict[str, Any]]:
        """All tools formatted for the Anthropic `tools` parameter."""
        return [t.to_anthropic_schema() for t in self._tools.values()]

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: object) -> bool:
        return name in self._tools


# --------------------------------------------------------------------------
# Input schemas for the seven standard tools.
#
# Why a model per tool rather than a shared one: Anthropic sends us the JSON
# schema of each tool to the model, so clarity here translates directly into
# fewer malformed tool calls at runtime.
# --------------------------------------------------------------------------


class _ListCampaignsInput(BaseModel):
    states: list[str] | None = Field(
        default=None,
        description=(
            "Optional filter; any of ON, OFF, SUSPENDED, ENDED, CONVERTED, "
            "ARCHIVED. Omit to return ON + SUSPENDED (the common 'active' set)."
        ),
    )


class _IdListInput(BaseModel):
    ids: list[int] = Field(..., description="One or more campaign ids.")


class _SetCampaignBudgetInput(BaseModel):
    campaign_id: int = Field(..., description="Target campaign id.")
    budget_rub: int = Field(
        ...,
        ge=300,
        description="New daily budget in rubles. Minimum 300 RUB (Direct's own floor).",
    )


class _GetKeywordsInput(BaseModel):
    adgroup_ids: list[int] = Field(
        ...,
        min_length=1,
        description="Ad-group ids whose keywords are returned. Required.",
    )


class _BidUpdateInput(BaseModel):
    keyword_id: int
    new_search_bid_rub: float | None = Field(
        default=None,
        description="New search-network bid in rubles. Omit to leave unchanged.",
    )
    new_network_bid_rub: float | None = Field(
        default=None,
        description="New content-network bid in rubles. Omit to leave unchanged.",
    )


class _SetKeywordBidsInput(BaseModel):
    updates: list[_BidUpdateInput] = Field(
        ...,
        min_length=1,
        description=(
            "Per-keyword bid changes. Ceiling: +50% per single call is enforced "
            "by the bidding service; violations are rejected."
        ),
    )


class _ValidatePhrasesInput(BaseModel):
    phrases: list[str] = Field(
        ...,
        min_length=1,
        description="Phrases to check for search volume via Direct's keywordsresearch.",
    )
    geo_ids: list[int] | None = Field(
        default=None, description="Optional Yandex region ids to scope the check."
    )


# --------------------------------------------------------------------------
# Standard tool factories.
#
# Each `_make_*_tool(settings)` builds one Tool with its settings captured
# in a closure, so handlers don't need to re-resolve config at call time.
# --------------------------------------------------------------------------


def _make_list_campaigns_tool(settings: Settings) -> Tool:
    async def handler(raw: BaseModel, ctx: ToolContext) -> Any:
        inp: _ListCampaignsInput = raw  # type: ignore[assignment]
        svc = CampaignService(settings)
        summaries = await svc.list_all() if inp.states is None else await svc.list_active()
        ctx.logger.info("tool.list_campaigns.ok", count=len(summaries))
        return [asdict(s) for s in summaries]

    return Tool(
        name="list_campaigns",
        description=(
            "List campaigns in the Yandex Direct account. Use this before any "
            "mutating operation to confirm ids/state. By default returns ON + "
            "SUSPENDED. Returns a list of {id, name, state, status, type, "
            "daily_budget_rub}."
        ),
        input_model=_ListCampaignsInput,
        is_write=False,
        handler=handler,
    )


def _make_pause_campaigns_tool(settings: Settings) -> Tool:
    async def handler(raw: BaseModel, ctx: ToolContext) -> Any:
        inp: _IdListInput = raw  # type: ignore[assignment]
        svc = CampaignService(settings)
        await svc.pause(inp.ids)
        ctx.logger.info("tool.pause_campaigns.ok", ids=inp.ids)
        return {"paused": inp.ids}

    return Tool(
        name="pause_campaigns",
        description=(
            "Pause (SUSPEND) one or more campaigns. Fully reversible via "
            "resume_campaigns. Safe default for 'stop spending on X' requests."
        ),
        input_model=_IdListInput,
        is_write=True,
        handler=handler,
    )


def _make_resume_campaigns_tool(settings: Settings) -> Tool:
    async def handler(raw: BaseModel, ctx: ToolContext) -> Any:
        inp: _IdListInput = raw  # type: ignore[assignment]
        svc = CampaignService(settings)
        await svc.resume(inp.ids)
        ctx.logger.info("tool.resume_campaigns.ok", ids=inp.ids)
        return {"resumed": inp.ids}

    return Tool(
        name="resume_campaigns",
        description=(
            "Resume (un-suspend) one or more campaigns. Starts spending again — "
            "confirm the daily budget before using."
        ),
        input_model=_IdListInput,
        is_write=True,
        handler=handler,
    )


def _make_set_campaign_budget_tool(settings: Settings) -> Tool:
    async def handler(raw: BaseModel, ctx: ToolContext) -> Any:
        inp: _SetCampaignBudgetInput = raw  # type: ignore[assignment]
        svc = CampaignService(settings)
        await svc.set_daily_budget(inp.campaign_id, inp.budget_rub)
        ctx.logger.info(
            "tool.set_campaign_budget.ok",
            campaign_id=inp.campaign_id,
            budget_rub=inp.budget_rub,
        )
        return {"campaign_id": inp.campaign_id, "budget_rub": inp.budget_rub}

    return Tool(
        name="set_campaign_budget",
        description=(
            "Set a campaign's daily budget in rubles. Direct's floor is 300 RUB; "
            "the service rejects below that. Raises are capped at +20% per call "
            "by policy; for larger changes, split across days."
        ),
        input_model=_SetCampaignBudgetInput,
        is_write=True,
        handler=handler,
    )


def _make_get_keywords_tool(settings: Settings) -> Tool:
    async def handler(raw: BaseModel, ctx: ToolContext) -> Any:
        inp: _GetKeywordsInput = raw  # type: ignore[assignment]
        async with DirectService(settings) as api:
            keywords = await api.get_keywords(inp.adgroup_ids)
        ctx.logger.info(
            "tool.get_keywords.ok",
            adgroup_ids=inp.adgroup_ids,
            count=len(keywords),
        )
        return [k.model_dump(by_alias=False, exclude_none=True) for k in keywords]

    return Tool(
        name="get_keywords",
        description=(
            "Return keywords for one or more ad groups. Use this before "
            "set_keyword_bids to sanity-check current bids and keyword state. "
            "Returns a list of {id, ad_group_id, keyword, state, status}."
        ),
        input_model=_GetKeywordsInput,
        is_write=False,
        handler=handler,
    )


def _make_set_keyword_bids_tool(settings: Settings) -> Tool:
    async def handler(raw: BaseModel, ctx: ToolContext) -> Any:
        inp: _SetKeywordBidsInput = raw  # type: ignore[assignment]
        svc = BiddingService(settings)
        updates = [
            BidUpdate(
                keyword_id=u.keyword_id,
                new_search_bid_rub=u.new_search_bid_rub,
                new_network_bid_rub=u.new_network_bid_rub,
            )
            for u in inp.updates
        ]
        await svc.apply(updates)
        ctx.logger.info("tool.set_keyword_bids.ok", count=len(updates))
        return {"updated": [u.keyword_id for u in updates]}

    return Tool(
        name="set_keyword_bids",
        description=(
            "Apply a batch of bid updates. Accepts per-keyword deltas "
            "(search-net and/or content-net). A single call may raise a bid by "
            "at most +50%. Bids are in rubles; the service converts to "
            "Direct's micro-currency units."
        ),
        input_model=_SetKeywordBidsInput,
        is_write=True,
        handler=handler,
    )


def _make_validate_phrases_tool(settings: Settings) -> Tool:
    async def handler(raw: BaseModel, ctx: ToolContext) -> Any:
        inp: _ValidatePhrasesInput = raw  # type: ignore[assignment]
        provider = DirectKeywordsResearch(settings)
        presence = await provider.has_search_volume(inp.phrases, inp.geo_ids)
        ctx.logger.info(
            "tool.validate_phrases.ok",
            checked=len(inp.phrases),
            with_volume=sum(1 for v in presence.values() if v),
        )
        return presence

    return Tool(
        name="validate_phrases",
        description=(
            "Check whether each of the given phrases has search volume on "
            "Yandex (via Direct's keywordsresearch.hasSearchVolume). Returns a "
            "map {phrase: bool}. Cheap and always-safe; use it to filter seed "
            "lists before creating keywords."
        ),
        input_model=_ValidatePhrasesInput,
        is_write=False,
        handler=handler,
    )


# --------------------------------------------------------------------------
# Public factory.
# --------------------------------------------------------------------------


_DEFAULT_FACTORIES: list[Callable[[Settings], Tool]] = [
    _make_list_campaigns_tool,
    _make_pause_campaigns_tool,
    _make_resume_campaigns_tool,
    _make_set_campaign_budget_tool,
    _make_get_keywords_tool,
    _make_set_keyword_bids_tool,
    _make_validate_phrases_tool,
]


def build_default_registry(settings: Settings) -> ToolRegistry:
    """Registry with the seven M1 tools bound to a Settings instance."""
    reg = ToolRegistry()
    for factory in _DEFAULT_FACTORIES:
        reg.add(factory(settings))
    return reg


# Re-export BidUpdate for convenience at the tools public surface — the agent
# tests commonly need it.
__all__ = [
    "BidUpdate",
    "Tool",
    "ToolContext",
    "ToolHandler",
    "ToolRegistry",
    "build_default_registry",
]
