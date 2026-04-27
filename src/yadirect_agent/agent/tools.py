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
from pydantic import BaseModel, ConfigDict, Field

from ..audit import AuditSink, JsonlSink
from ..clients.direct import DirectService
from ..clients.wordstat import DirectKeywordsResearch
from ..config import Settings
from ..services.bidding import BiddingService, BidUpdate
from ..services.campaigns import CampaignService
from .executor import PlanRejected, PlanRequired
from .pipeline import SafetyPipeline
from .plans import PendingPlansStore
from .safety import (
    BudgetCapPolicy,
    ConversionIntegrityPolicy,
    MaxCpcPolicy,
    Policy,
    QueryDriftPolicy,
    load_policy,
)

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


# --------------------------------------------------------------------------
# Privacy: keys we MUST strip from CheckResult.details before returning a
# rejection to the LLM agent. KS#7 (query drift) populates
# ``new_queries_sample`` with raw user search queries that may carry
# names, addresses, medical phrases, etc. The audit sink (M2.3, not yet
# shipped) is the right place to redact for log persistence; until then
# we redact at the tool boundary so the raw queries never reach the LLM
# context (where the API provider may retain them). Auditor PR-B1
# second-pass MEDIUM.
# --------------------------------------------------------------------------

_PRIVATE_DETAIL_KEYS: frozenset[str] = frozenset({"new_queries_sample"})


def _redact_details(details: dict[str, Any]) -> dict[str, Any]:
    """Drop privacy-sensitive keys from a CheckResult.details dict."""
    return {k: v for k, v in details.items() if k not in _PRIVATE_DETAIL_KEYS}


# Every tool input model uses ``extra="forbid"`` as defence-in-depth: a
# silently-accepted unknown key today would be a wire vector tomorrow if
# any handler ever forwarded fields into the wrapped service. Concretely
# this prevents an LLM from sneaking ``_applying_plan_id`` into the
# tool-call JSON to bypass the @requires_plan gate (auditor HIGH-2).
_STRICT = ConfigDict(extra="forbid")


class _ListCampaignsInput(BaseModel):
    model_config = _STRICT

    states: list[str] | None = Field(
        default=None,
        description=(
            "Optional filter; any of ON, OFF, SUSPENDED, ENDED, CONVERTED, "
            "ARCHIVED. Omit to return ON + SUSPENDED (the common 'active' set)."
        ),
    )


class _IdListInput(BaseModel):
    model_config = _STRICT

    ids: list[int] = Field(..., description="One or more campaign ids.")


class _SetCampaignBudgetInput(BaseModel):
    model_config = _STRICT

    campaign_id: int = Field(..., description="Target campaign id.")
    budget_rub: int = Field(
        ...,
        ge=300,
        description="New daily budget in rubles. Minimum 300 RUB (Direct's own floor).",
    )


class _GetKeywordsInput(BaseModel):
    model_config = _STRICT

    adgroup_ids: list[int] = Field(
        ...,
        min_length=1,
        description="Ad-group ids whose keywords are returned. Required.",
    )


class _BidUpdateInput(BaseModel):
    model_config = _STRICT

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
    model_config = _STRICT

    updates: list[_BidUpdateInput] = Field(
        ...,
        min_length=1,
        description=(
            "Per-keyword bid changes. Ceiling: +50% per single call is enforced "
            "by the bidding service; violations are rejected."
        ),
    )


class _ValidatePhrasesInput(BaseModel):
    model_config = _STRICT

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


def _make_list_campaigns_tool(
    settings: Settings,
    pipeline: SafetyPipeline,
    store: PendingPlansStore,
    audit_sink: AuditSink,
) -> Tool:
    async def handler(raw: BaseModel, ctx: ToolContext) -> Any:
        inp: _ListCampaignsInput = raw  # type: ignore[assignment]
        svc = CampaignService(settings, pipeline=pipeline, store=store, audit_sink=audit_sink)
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


def _make_pause_campaigns_tool(
    settings: Settings,
    pipeline: SafetyPipeline,
    store: PendingPlansStore,
    audit_sink: AuditSink,
) -> Tool:
    async def handler(raw: BaseModel, ctx: ToolContext) -> Any:
        inp: _IdListInput = raw  # type: ignore[assignment]
        svc = CampaignService(settings, pipeline=pipeline, store=store, audit_sink=audit_sink)
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


def _make_resume_campaigns_tool(
    settings: Settings,
    pipeline: SafetyPipeline,
    store: PendingPlansStore,
    audit_sink: AuditSink,
) -> Tool:
    async def handler(raw: BaseModel, ctx: ToolContext) -> Any:
        inp: _IdListInput = raw  # type: ignore[assignment]
        svc = CampaignService(settings, pipeline=pipeline, store=store, audit_sink=audit_sink)
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


def _make_set_campaign_budget_tool(
    settings: Settings,
    pipeline: SafetyPipeline,
    store: PendingPlansStore,
    audit_sink: AuditSink,
) -> Tool:
    async def handler(raw: BaseModel, ctx: ToolContext) -> Any:
        inp: _SetCampaignBudgetInput = raw  # type: ignore[assignment]
        svc = CampaignService(settings, pipeline=pipeline, store=store, audit_sink=audit_sink)
        try:
            await svc.set_daily_budget(inp.campaign_id, inp.budget_rub)
        except PlanRequired as exc:
            # Pipeline returned ``confirm`` — operator must run apply-plan.
            # Surface the plan_id back to the agent so it can include the
            # next step in its message to the user.
            ctx.logger.info(
                "tool.set_campaign_budget.pending",
                campaign_id=inp.campaign_id,
                budget_rub=inp.budget_rub,
                plan_id=exc.plan_id,
            )
            return {
                "status": "pending",
                "plan_id": exc.plan_id,
                "preview": exc.preview,
                "reason": exc.reason,
                "next_step": (
                    f"Operator approval required. Run "
                    f"`yadirect-agent apply-plan {exc.plan_id}` to confirm."
                ),
            }
        except PlanRejected as exc:
            # Pipeline returned ``reject`` — surface enough detail for the
            # agent to explain to the user why it can't proceed without
            # leaking internal check names verbatim.
            ctx.logger.info(
                "tool.set_campaign_budget.rejected",
                campaign_id=inp.campaign_id,
                budget_rub=inp.budget_rub,
                reason=exc.reason,
            )
            return {
                "status": "rejected",
                "reason": exc.reason,
                # ``details`` carries the numerical context (projected
                # totals, cap thresholds, etc.) the agent needs to
                # explain *why* to the user. Include them — but route
                # through ``_redact_details`` first so privacy-sensitive
                # keys (e.g. KS#7 raw user queries) never reach the
                # LLM context. Auditor LOW + second-pass MEDIUM.
                "blocking": [
                    {
                        "status": r.status,
                        "reason": r.reason,
                        "details": _redact_details(r.details),
                    }
                    for r in exc.blocking
                ],
            }
        ctx.logger.info(
            "tool.set_campaign_budget.ok",
            campaign_id=inp.campaign_id,
            budget_rub=inp.budget_rub,
        )
        return {
            "status": "applied",
            "campaign_id": inp.campaign_id,
            "budget_rub": inp.budget_rub,
        }

    return Tool(
        name="set_campaign_budget",
        description=(
            "Set a campaign's daily budget in rubles. Direct's floor is 300 RUB; "
            "the service rejects below that. Most budget changes return "
            "{status: 'pending', plan_id: ...} requiring operator approval via "
            "`yadirect-agent apply-plan <id>` — relay that step to the user. "
            "If status='applied' the change is live; if status='rejected' it "
            "violated the safety policy (see reason + blocking)."
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


def _apply_env_backstop(policy: Policy, settings: Settings) -> Policy:
    """Tighten the Policy's account budget cap with the env-level
    backstop ``settings.agent_max_daily_budget_rub`` (M2.4).

    The function is purely-functional: returns either the original
    ``policy`` (when the env is loose enough that ``min`` picks the
    YAML) or a deep-copied ``Policy`` with a new
    ``budget_cap.account_daily_budget_cap_rub``.

    Logs a structured ``env_backstop`` warning whenever it actually
    tightens — silent tightening would be a "why is the agent
    rejecting valid budgets" debugging trap.
    """
    yaml_cap = policy.budget_cap.account_daily_budget_cap_rub
    env_cap = settings.agent_max_daily_budget_rub
    effective = min(yaml_cap, env_cap)
    if effective == yaml_cap:
        return policy

    structlog.get_logger(__name__).warning(
        "env_backstop_tightening_account_cap",
        yaml_cap_rub=yaml_cap,
        env_cap_rub=env_cap,
        effective_cap_rub=effective,
        note=(
            "AGENT_MAX_DAILY_BUDGET_RUB is tighter than the YAML's "
            "account_daily_budget_cap_rub; env wins. Mutations now "
            "reject above the env ceiling."
        ),
    )
    new_budget_cap = policy.budget_cap.model_copy(
        update={"account_daily_budget_cap_rub": effective}
    )
    return policy.model_copy(update={"budget_cap": new_budget_cap})


def build_safety_pair(
    settings: Settings,
) -> tuple[SafetyPipeline, PendingPlansStore, JsonlSink]:
    """Construct the shared ``(SafetyPipeline, PendingPlansStore, JsonlSink)``
    triple that every CampaignService instance in this registry will consume.

    Constructed once per ``build_default_registry`` call so the
    ``SessionState`` (cross-tool TOCTOU register inside the pipeline)
    persists across tool calls within one agent run.

    Policy resolution: read ``settings.agent_policy_path`` if it exists,
    otherwise fall back to a Policy whose mandatory account cap is
    seeded from ``settings.agent_max_daily_budget_rub`` (the M2.4 env
    backstop, default 10_000 RUB). Every slice uses its conservative
    built-in defaults. The fallback is intentional — a missing policy
    file is normal during early bring-up and in CI where the file is
    not committed; it should not prevent the agent from booting.
    Operators wanting custom thresholds drop a YAML at the configured
    path.
    """

    if settings.agent_policy_path.exists():
        policy: Policy = load_policy(settings.agent_policy_path)
    else:
        # Auditor MEDIUM: silent fallback is operationally invisible — an
        # operator with a typo in AGENT_POLICY_PATH would never know why
        # the agent returns rollout-stage rejections. Log the path we
        # were looking for so the structured-log search resolves it.
        # NB: with default rollout_stage="shadow" mutations are *rejected*
        # in this fallback (intentionally — feature is off until configured).
        structlog.get_logger(__name__).warning(
            "policy_file_not_found",
            path=str(settings.agent_policy_path),
            note=(
                "using default Policy with rollout_stage='shadow' "
                "(read-only); mutations will be rejected until a real "
                "agent_policy.yml is provided."
            ),
        )
        policy = Policy(
            budget_cap=BudgetCapPolicy(
                account_daily_budget_cap_rub=settings.agent_max_daily_budget_rub,
            ),
            max_cpc=MaxCpcPolicy(),
            query_drift=QueryDriftPolicy(),
            conversion_integrity=ConversionIntegrityPolicy(
                min_conversions_total=1,
                min_ratio_vs_baseline=0.5,
                require_all_baseline_goals_present=True,
            ),
        )

    # M2.4 daily-budget hard guard: ``AGENT_MAX_DAILY_BUDGET_RUB``
    # is the deployment-time ceiling. If it's tighter than the
    # YAML's ``account_daily_budget_cap_rub`` (typo / stale
    # checkout / generous YAML in dev that leaked to staging), the
    # env wins. We tighten the Policy at build time rather than
    # adding a second check — KS#1 BudgetCapCheck stays the single
    # enforcement point with one audit reason; the env is just one
    # more input into the cap. ``min`` always picks the safer
    # number; if the YAML is already tighter, this is a no-op.
    policy = _apply_env_backstop(policy, settings)

    pipeline = SafetyPipeline(policy)
    store = PendingPlansStore(settings.audit_log_path.parent / "pending_plans.jsonl")
    audit_sink = JsonlSink(settings.audit_log_path)
    return pipeline, store, audit_sink


# Two flavours of factory live in the registry's default set:
#   - ``Settings``-only factories (read-only / non-CampaignService tools)
#   - ``Settings + pipeline + store`` factories (CampaignService-backed tools)
# We dispatch at registry-build time. Listing them in a single tuple keeps
# the order stable for diffing.

_CampaignFactory = Callable[[Settings, SafetyPipeline, PendingPlansStore, AuditSink], Tool]
_PlainFactory = Callable[[Settings], Tool]

_CAMPAIGN_FACTORIES: list[_CampaignFactory] = [
    _make_list_campaigns_tool,
    _make_pause_campaigns_tool,
    _make_resume_campaigns_tool,
    _make_set_campaign_budget_tool,
]
_PLAIN_FACTORIES: list[_PlainFactory] = [
    _make_get_keywords_tool,
    _make_set_keyword_bids_tool,
    _make_validate_phrases_tool,
]


def build_default_registry(settings: Settings) -> ToolRegistry:
    """Registry with the seven M1 tools bound to a Settings instance.

    Builds a single ``SafetyPipeline`` + ``PendingPlansStore`` pair
    shared by every CampaignService-backed handler so the pipeline's
    SessionState (cross-tool TOCTOU register) survives across tool
    calls within one agent run.
    """
    pipeline, store, audit_sink = build_safety_pair(settings)
    reg = ToolRegistry()
    for cf in _CAMPAIGN_FACTORIES:
        reg.add(cf(settings, pipeline, store, audit_sink))
    for pf in _PLAIN_FACTORIES:
        reg.add(pf(settings))
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
    "build_safety_pair",
]
