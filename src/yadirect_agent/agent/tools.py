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

from collections.abc import Awaitable, Callable, Iterator
from dataclasses import asdict, dataclass
from typing import Any

import structlog
from pydantic import BaseModel, ConfigDict, Field

from ..audit import AuditSink, JsonlSink
from ..clients.direct import DirectService
from ..clients.wordstat import DirectKeywordsResearch
from ..config import Settings
from ..rollout import RolloutStateStore
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

    def __iter__(self) -> Iterator[Tool]:
        """Iterate over registered tools in insertion order.

        Used by the MCP server adapter (M3) to walk the registry
        without lifting private state.
        """
        return iter(self._tools.values())


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

_PRIVATE_DETAIL_KEYS: frozenset[str] = frozenset(
    {
        # KS#7 (query drift) — raw user search terms reach the agent
        # via ``new_queries_sample`` in CheckResult.details. Direct
        # search terms can carry names, addresses, medical phrases.
        "new_queries_sample",
        # KS#3 (negative-keyword floor) — the operator-supplied list
        # of required phrases the campaign lacks. Commercial intent
        # (competitor names / brand misspells / regulated-product
        # filters) that has no business reaching the LLM. The audit
        # sink already strips this via ``audit._PRIVATE_KEYS``;
        # mirroring it here matches the audit-facing channel to the
        # agent-facing channel. Auditor M2-ks3-negatives HIGH-1.
        "missing",
    }
)


def _redact_details(details: dict[str, Any]) -> dict[str, Any]:
    """Drop privacy-sensitive keys from a CheckResult.details dict."""
    return {k: v for k, v in details.items() if k not in _PRIVATE_DETAIL_KEYS}


def _pending_response(exc: PlanRequired) -> dict[str, Any]:
    """Structured tool response for the ``confirm`` path.

    The agent relays ``next_step`` to the user verbatim so they
    know exactly which command to run.
    """

    return {
        "status": "pending",
        "plan_id": exc.plan_id,
        "preview": exc.preview,
        "reason": exc.reason,
        "next_step": (
            f"Operator approval required. Run `yadirect-agent apply-plan {exc.plan_id}` to confirm."
        ),
    }


def _rejected_response(exc: PlanRejected) -> dict[str, Any]:
    """Structured tool response for the ``reject`` path.

    Each blocking ``CheckResult.details`` dict is routed through
    ``_redact_details`` so privacy-sensitive keys (KS#7 raw user
    queries, KS#3 negative-keyword phrases) never reach the LLM
    context.
    """

    return {
        "status": "rejected",
        "reason": exc.reason,
        "blocking": [
            {
                "status": r.status,
                "reason": r.reason,
                "details": _redact_details(r.details),
            }
            for r in exc.blocking
        ],
    }


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


_REASON_FIELD_DESCRIPTION = (
    "REQUIRED: one-to-two-sentence reason for THIS action. Explain WHY, "
    "not WHAT (the args already cover what). The reason is recorded as "
    "the rationale summary the operator can read back later "
    "('why did you do X yesterday?') — be specific and grounded in the "
    "data you observed. Examples: 'CTR < 0.5% over last 7 days, no "
    "conversions.' / 'CPA below target for 5 consecutive days, scaling.' "
    "/ 'Top-converting keyword, raising bid by 10%.'"
)


class _IdListInput(BaseModel):
    model_config = _STRICT

    ids: list[int] = Field(
        ...,
        min_length=1,
        description="One or more campaign ids.",
    )
    reason: str = Field(
        ...,
        min_length=10,
        max_length=500,
        description=_REASON_FIELD_DESCRIPTION,
    )


class _SetCampaignBudgetInput(BaseModel):
    model_config = _STRICT

    campaign_id: int = Field(..., description="Target campaign id.")
    budget_rub: int = Field(
        ...,
        ge=300,
        description="New daily budget in rubles. Minimum 300 RUB (Direct's own floor).",
    )
    reason: str = Field(
        ...,
        min_length=10,
        max_length=500,
        description=_REASON_FIELD_DESCRIPTION,
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
    reason: str = Field(
        ...,
        min_length=10,
        max_length=500,
        description=_REASON_FIELD_DESCRIPTION,
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
        try:
            await svc.pause(inp.ids)
        except PlanRequired as exc:
            ctx.logger.info("tool.pause_campaigns.pending", ids=inp.ids, plan_id=exc.plan_id)
            return _pending_response(exc)
        except PlanRejected as exc:
            ctx.logger.info("tool.pause_campaigns.rejected", ids=inp.ids, reason=exc.reason)
            return _rejected_response(exc)
        ctx.logger.info("tool.pause_campaigns.ok", ids=inp.ids)
        return {"status": "applied", "paused": inp.ids}

    return Tool(
        name="pause_campaigns",
        description=(
            "Pause (SUSPEND) one or more campaigns. Fully reversible via "
            "resume_campaigns. Returns {status: 'applied' | 'pending' | "
            "'rejected', ...}; 'pending' carries plan_id and the operator's "
            "next step (apply-plan)."
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
        try:
            await svc.resume(inp.ids)
        except PlanRequired as exc:
            ctx.logger.info("tool.resume_campaigns.pending", ids=inp.ids, plan_id=exc.plan_id)
            return _pending_response(exc)
        except PlanRejected as exc:
            ctx.logger.info("tool.resume_campaigns.rejected", ids=inp.ids, reason=exc.reason)
            return _rejected_response(exc)
        ctx.logger.info("tool.resume_campaigns.ok", ids=inp.ids)
        return {"status": "applied", "resumed": inp.ids}

    return Tool(
        name="resume_campaigns",
        description=(
            "Resume (un-suspend) one or more campaigns. Starts spending again — "
            "by default returns {status: 'pending', plan_id} requiring "
            "operator approval via `yadirect-agent apply-plan <id>`. Resume "
            "is the primary trigger for KS#3 (negative-keyword floor); "
            "rejected if any campaign lacks the configured required "
            "negative keywords."
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
            ctx.logger.info(
                "tool.set_campaign_budget.pending",
                campaign_id=inp.campaign_id,
                budget_rub=inp.budget_rub,
                plan_id=exc.plan_id,
            )
            return _pending_response(exc)
        except PlanRejected as exc:
            ctx.logger.info(
                "tool.set_campaign_budget.rejected",
                campaign_id=inp.campaign_id,
                budget_rub=inp.budget_rub,
                reason=exc.reason,
            )
            return _rejected_response(exc)
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


def _make_set_keyword_bids_tool(
    settings: Settings,
    pipeline: SafetyPipeline,
    store: PendingPlansStore,
    audit_sink: AuditSink,
) -> Tool:
    async def handler(raw: BaseModel, ctx: ToolContext) -> Any:
        inp: _SetKeywordBidsInput = raw  # type: ignore[assignment]
        svc = BiddingService(settings, pipeline=pipeline, store=store, audit_sink=audit_sink)
        updates = [
            BidUpdate(
                keyword_id=u.keyword_id,
                new_search_bid_rub=u.new_search_bid_rub,
                new_network_bid_rub=u.new_network_bid_rub,
            )
            for u in inp.updates
        ]
        try:
            await svc.apply(updates)
        except PlanRequired as exc:
            ctx.logger.info(
                "tool.set_keyword_bids.pending",
                count=len(updates),
                plan_id=exc.plan_id,
            )
            return _pending_response(exc)
        except PlanRejected as exc:
            ctx.logger.info(
                "tool.set_keyword_bids.rejected",
                count=len(updates),
                reason=exc.reason,
            )
            return _rejected_response(exc)
        ctx.logger.info("tool.set_keyword_bids.ok", count=len(updates))
        return {"status": "applied", "updated": [u.keyword_id for u in updates]}

    return Tool(
        name="set_keyword_bids",
        description=(
            "Apply a batch of bid updates. Accepts per-keyword deltas "
            "(search-net and/or content-net). A single call may raise a bid by "
            "at most +50%. Bids are in rubles; the service converts to "
            "Direct's micro-currency units. Returns "
            "{status: 'pending'|'rejected'|'applied', ...}; 'pending' "
            "carries plan_id and the operator's apply-plan command."
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


def _apply_rollout_state_override(policy: Policy, settings: Settings) -> Policy:
    """Override ``Policy.rollout_stage`` from the on-disk
    rollout-state file when present (M2.5).

    Layering:
    - YAML (``Policy.rollout_stage``) is the default.
    - ``rollout_state.json`` next to the audit log (written by the
      ``yadirect-agent rollout promote`` CLI command) takes
      precedence.

    Returns the original ``policy`` unchanged when:
    - the state-file is missing (fresh deployment), or
    - the persisted stage matches the YAML stage (no-op).

    Otherwise returns a deep-copied Policy with the new stage.
    """
    state_path = settings.audit_log_path.parent / "rollout_state.json"
    state = RolloutStateStore(state_path).load()
    if state is None:
        return policy
    if state.stage == policy.rollout_stage:
        return policy

    structlog.get_logger(__name__).info(
        "rollout_state_override",
        yaml_stage=policy.rollout_stage,
        state_file_stage=state.stage,
        promoted_at=state.promoted_at.isoformat(),
        promoted_by=state.promoted_by,
    )
    return policy.model_copy(update={"rollout_stage": state.stage})


def _apply_env_backstop(policy: Policy, settings: Settings) -> Policy:
    """Tighten the Policy's account budget cap with the env-level
    backstop ``settings.agent_max_daily_budget_rub`` (M2.4).

    The function is purely-functional: returns either the original
    ``policy`` (when the env is loose enough that ``min`` picks the
    YAML) or a deep-copied ``Policy`` with a new
    ``budget_cap.account_daily_budget_cap_rub``.

    Boot-time only: applied once at ``build_safety_pair`` time and
    captured into the ``SafetyPipeline``. Changing the env at
    runtime requires a process restart to take effect. This is
    intentional — boot-time application avoids race conditions on a
    per-request cap and is consistent with how Settings itself is
    loaded once at entry-point.

    Always logs an INFO line ``budget_cap_resolved`` so operators
    have a "what cap is the agent using right now" line in startup
    logs even when YAML wins; additionally emits a WARNING-level
    ``env_backstop_tightening_account_cap`` whenever the env actually
    tightens (auditor M2.4 LOW L1).

    NB: bid changes reach KS#2 (per-keyword max-CPC), not KS#1's
    daily-budget projection. A higher bid burns the budget faster
    but does not raise the daily ceiling, so the env-backstop here
    only affects budget-change / resume / archive paths through
    KS#1 — which is the spec's "any operation that may raise daily
    spend" set in practice (auditor M2.4 LOW L3).

    NB: in the no-YAML fallback path (see ``build_safety_pair``),
    ``yaml_cap`` is itself seeded from ``env_cap``, so this function
    is a structural no-op there — both inputs to ``min`` are equal
    by construction. Auditor M2.4 LOW L2.
    """
    yaml_cap = policy.budget_cap.account_daily_budget_cap_rub
    env_cap = settings.agent_max_daily_budget_rub
    effective = min(yaml_cap, env_cap)

    log = structlog.get_logger(__name__)
    log.info(
        "budget_cap_resolved",
        yaml_cap_rub=yaml_cap,
        env_cap_rub=env_cap,
        effective_cap_rub=effective,
    )

    if effective == yaml_cap:
        return policy

    log.warning(
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
    # M2.5 staged rollout: if an operator has run
    # ``yadirect-agent rollout promote --to <stage>``, the persisted
    # state-file overrides the YAML's ``rollout_stage``. Apply AFTER
    # env-backstop so the budget-cap and rollout-stage decisions are
    # both finalised before the SafetyPipeline is constructed.
    policy = _apply_rollout_state_override(policy, settings)

    pipeline = SafetyPipeline(policy)
    store = PendingPlansStore(settings.audit_log_path.parent / "pending_plans.jsonl")
    audit_sink = JsonlSink(settings.audit_log_path)
    return pipeline, store, audit_sink


# Two flavours of factory live in the registry's default set:
#   - ``Settings``-only factories (read-only / pure-research tools)
#   - ``Settings + pipeline + store + audit_sink`` factories (any tool
#     whose service runs through the safety pipeline — CampaignService
#     mutations AND BiddingService.apply, the latter wired in as part
#     of the M2 pause/resume/bid gating follow-up).
# We dispatch at registry-build time. Listing them in a single tuple keeps
# the order stable for diffing.

_GatedFactory = Callable[[Settings, SafetyPipeline, PendingPlansStore, AuditSink], Tool]
_PlainFactory = Callable[[Settings], Tool]

_GATED_FACTORIES: list[_GatedFactory] = [
    _make_list_campaigns_tool,
    _make_pause_campaigns_tool,
    _make_resume_campaigns_tool,
    _make_set_campaign_budget_tool,
    _make_set_keyword_bids_tool,
]
_PLAIN_FACTORIES: list[_PlainFactory] = [
    _make_get_keywords_tool,
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
    for cf in _GATED_FACTORIES:
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
