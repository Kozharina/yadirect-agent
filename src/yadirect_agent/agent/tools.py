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
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..audit import AuditSink, JsonlSink
from ..auth.keychain import KeyringTokenStore
from ..clients.direct import DirectService
from ..clients.wordstat import DirectKeywordsResearch
from ..config import Settings
from ..exceptions import ConfigError
from ..models.health import default_window, health_report_to_jsonable_dict
from ..models.rationale import Rationale
from ..rollout import RolloutStateStore
from ..services.bidding import BiddingService, BidUpdate
from ..services.campaigns import CampaignService
from ..services.health_check import HealthCheckService
from .executor import PlanRejected, PlanRequired
from .pipeline import SafetyPipeline
from .plans import PendingPlansStore
from .rationale_store import RationaleStore
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

# Placeholder ``decision_id`` the handler stamps onto each Rationale.
# The @requires_plan decorator overwrites it with ``plan.plan_id`` at
# emit time (see ``executor._emit_rationale``). Pinned here as a
# constant so a future "let me trace the placeholder through the
# pipeline" debugger has one greppable surface; the validator on
# ``Rationale.decision_id`` requires non-whitespace + non-empty,
# which this satisfies.
_RATIONALE_PLACEHOLDER_ID = "pending"


def _build_handler_rationale(
    *,
    action: str,
    resource_type: str,
    resource_ids: list[int],
    reason: str,
) -> Rationale:
    """Build a Rationale from a tool handler's input ``reason``.

    The handler holds the agent's articulated reason at the call
    site; everything else (action, resource_type, resource_ids)
    mirrors the @requires_plan configuration verbatim so a future
    ``rationale list --action=set_campaign_budget`` query lines up
    with the operator-facing identifiers in ``plans list``.

    ``inputs``, ``alternatives_considered``, and ``policy_slack``
    stay empty in slice 2 — slice 4 auto-populates ``policy_slack``
    from ``CheckResult.details`` inside the safety pipeline; the
    other two stay empty until a future agent-loop slice surfaces
    structured reasoning artefacts.
    """
    return Rationale(
        decision_id=_RATIONALE_PLACEHOLDER_ID,
        action=action,
        resource_type=resource_type,
        resource_ids=resource_ids,
        summary=reason,
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


class _ExplainDecisionInput(BaseModel):
    model_config = _STRICT

    decision_id: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description=(
            "The decision_id of a previously-recorded rationale. Same id "
            "as ``OperationPlan.plan_id`` for plans the agent created. "
            "Get it from a previous tool response (``plan_id`` field on "
            "any pending/applied/rejected response), from the operator's "
            "``yadirect-agent rationale list`` output, or from "
            "``yadirect-agent plans list``. NEVER fabricate one — call "
            "this tool only when the user references a specific past "
            "decision."
        ),
    )

    @field_validator("decision_id")
    @classmethod
    def _no_whitespace(cls, v: str) -> str:
        # Same constraint as ``Rationale.decision_id`` (M20.1 MEDIUM-2)
        # and ``OperationPlan.plan_id`` — pinning at the tool boundary
        # too means a query with stray whitespace fails up front
        # rather than silently returning a misleading "not found".
        if any(ch.isspace() for ch in v):
            msg = "decision_id must not contain whitespace"
            raise ValueError(msg)
        return v


class _AccountHealthInput(BaseModel):
    model_config = _STRICT

    days: int = Field(
        default=7,
        ge=1,
        le=90,
        description=(
            "Window length in days, ending YESTERDAY (Metrika in-flight-day "
            "data is incomplete and lags by hours; rule decisions on partial "
            "data tend to false-positive). Default 7 mirrors the CLI default "
            "and matches the operator's natural 'how was last week' question. "
            "Cap at 90 because longer windows dilute today's signals into "
            "noise."
        ),
    )
    goal_id: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Metrika goal id to count conversions against. Without it, "
            "conversion-based rules (high-CPA, etc.) silently skip — they "
            "have no reference to compute against. Get the id from "
            "``yadirect-agent doctor`` or the operator's Metrika dashboard. "
            "Optional — runs cost-only rules without it."
        ),
    )


class _StartOnboardingInput(BaseModel):
    model_config = _STRICT

    # Slice 1 takes no fields — the first call from the LLM
    # ("помоги настроить агента") must succeed with zero context.
    # ``extra="forbid"`` (inherited from ``_STRICT``) still rejects
    # unknown keys, so a future attempt to sneak in
    # ``_force_ready=True`` cannot bypass the OAuth probe. Slice 2
    # will add an optional ``answers: dict[str, Any]`` for the
    # BusinessProfile Q&A state machine.


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
        rationale = _build_handler_rationale(
            action="pause_campaigns",
            resource_type="campaign",
            resource_ids=list(inp.ids),
            reason=inp.reason,
        )
        svc = CampaignService(settings, pipeline=pipeline, store=store, audit_sink=audit_sink)
        try:
            await svc.pause(inp.ids, rationale=rationale)
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
        rationale = _build_handler_rationale(
            action="resume_campaigns",
            resource_type="campaign",
            resource_ids=list(inp.ids),
            reason=inp.reason,
        )
        svc = CampaignService(settings, pipeline=pipeline, store=store, audit_sink=audit_sink)
        try:
            await svc.resume(inp.ids, rationale=rationale)
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
        rationale = _build_handler_rationale(
            action="set_campaign_budget",
            resource_type="campaign",
            resource_ids=[inp.campaign_id],
            reason=inp.reason,
        )
        svc = CampaignService(settings, pipeline=pipeline, store=store, audit_sink=audit_sink)
        try:
            await svc.set_daily_budget(inp.campaign_id, inp.budget_rub, rationale=rationale)
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
        rationale = _build_handler_rationale(
            action="set_keyword_bids",
            resource_type="keyword",
            resource_ids=[u.keyword_id for u in inp.updates],
            reason=inp.reason,
        )
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
            await svc.apply(updates, rationale=rationale)
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


def _rationale_store_path(settings: Settings) -> Any:
    """Standard location: sibling to the audit log.

    Mirrors ``cli/main.py:_rationale_store`` so both call sites pick
    up the same on-disk file (and pin a stable JSONL path the
    operator can grep / archive). A future refactor that promotes
    this to a ``RationaleStore.from_settings`` classmethod is
    BACKLOG'd; one-line duplication is cheaper than a cross-module
    helper for two callers.
    """
    return settings.audit_log_path.parent / "rationale.jsonl"


def _make_explain_decision_tool(settings: Settings) -> Tool:
    """Read-back tool for recorded rationales (M20 slice 3).

    The closing slice of M20: slice 1 added the model + JSONL store,
    slice 2 made emission hard-required so every plan has a recorded
    rationale, slice 3 (this) exposes those records to the LLM.
    Reading is cheap and read-only — no pipeline / store / audit_sink
    needed. ``RationaleStore`` itself is stateless (just a Path
    wrapper); construction is microseconds.
    """

    async def handler(raw: BaseModel, ctx: ToolContext) -> Any:
        inp: _ExplainDecisionInput = raw  # type: ignore[assignment]
        store = RationaleStore(_rationale_store_path(settings))
        rationale = store.get(inp.decision_id)
        if rationale is None:
            ctx.logger.info(
                "tool.explain_decision.not_found",
                decision_id=inp.decision_id,
            )
            return {"status": "not_found", "decision_id": inp.decision_id}
        ctx.logger.info(
            "tool.explain_decision.found",
            decision_id=inp.decision_id,
            action=rationale.action,
        )
        # ``mode="json"`` renders datetimes as ISO strings and the
        # Confidence enum as its string value — JSON-only payloads
        # over MCP cannot carry Python types.
        return {"status": "found", "rationale": rationale.model_dump(mode="json")}

    return Tool(
        name="explain_decision",
        description=(
            "Retrieve the recorded rationale for a specific past decision — "
            "what the agent decided, why, what data it used, what "
            "alternatives it rejected, how confident it was, and how close "
            "the decision was to safety thresholds. Use this when the user "
            "asks WHY the agent did X earlier; NEVER fabricate a reason — "
            "always pull the recorded one. Returns {status: 'found', "
            "rationale: {decision_id, timestamp, action, resource_type, "
            "resource_ids, summary, inputs, alternatives_considered, "
            "policy_slack, confidence}} or {status: 'not_found', "
            "decision_id} when the id is unknown. Pair with `plans list` "
            "(CLI) or any previous tool response that returned a plan_id."
        ),
        input_model=_ExplainDecisionInput,
        is_write=False,
        handler=handler,
    )


def _make_account_health_tool(settings: Settings) -> Tool:
    """Rule-based account-health check exposed over MCP (M15.5).

    Mirrors the existing ``yadirect-agent health`` CLI: deterministic
    rules over Metrika + Direct data, no LLM involved. Reuses
    ``HealthCheckService`` verbatim — no new readers, no new rules.
    Read-only by definition; joins the read-only catalogue exposed
    in default MCP mode without operator opt-in.
    """

    async def handler(raw: BaseModel, ctx: ToolContext) -> Any:
        inp: _AccountHealthInput = raw  # type: ignore[assignment]
        date_range = default_window(days=inp.days)
        try:
            async with HealthCheckService(settings) as svc:
                report = await svc.run_account_check(
                    date_range=date_range,
                    goal_id=inp.goal_id,
                )
        except ConfigError as exc:
            # Most common deployment-time failure: ``YANDEX_METRIKA_COUNTER_ID``
            # not set. Surface as structured data the LLM can act on
            # (tell the user which env var to set) instead of letting
            # the exception bubble up as a generic tool error.
            ctx.logger.info(
                "tool.account_health.unconfigured",
                reason=str(exc),
            )
            return {"status": "unconfigured", "reason": str(exc)}

        ctx.logger.info(
            "tool.account_health.ok",
            findings=len(report.findings),
            days=inp.days,
            goal_id=inp.goal_id,
        )
        # Findings returned in their natural order — the LLM can
        # sort / group as needed. CLI-side sorting (severity desc,
        # impact desc, campaign id asc) lives in
        # ``cli/health.py:render_report_json`` for terminal scanning.
        return {"status": "ok", "report": health_report_to_jsonable_dict(report)}

    return Tool(
        name="account_health",
        description=(
            "Run a deterministic, rule-based health check on the Yandex.Direct "
            "account and return a structured list of findings (burning campaigns "
            "with no conversions, high-CPA campaigns above target, more rules "
            "as M15.5 grows). NO LLM involved — purely Metrika + Direct data. "
            "Use this when the user asks 'how is my account?', 'what should I "
            "fix?', 'any warnings?', or after a config change. Returns "
            "{status: 'ok', report: {date_range: {start, end}, findings: "
            "[{rule_id, severity, campaign_id, campaign_name, message, "
            "estimated_impact_rub}]}}; or {status: 'unconfigured', reason: "
            "...} when YANDEX_METRIKA_COUNTER_ID is not set (tell the user "
            "to set it). ``goal_id`` (Metrika goal) is optional — without it, "
            "conversion-based rules silently skip. Default 7-day window, "
            "ending yesterday."
        ),
        input_model=_AccountHealthInput,
        is_write=False,
        handler=handler,
    )


def _make_start_onboarding_tool(settings: Settings) -> Tool:
    """Conversational onboarding entry point (M15.4 slice 1).

    First, minimal cut: probes OAuth state via ``KeyringTokenStore``
    and returns a structured next-step. Slices 2-5 layer the
    BusinessProfile Q&A, policy proposal, baseline snapshot, and
    first health-check on top of this skeleton.

    Branches:
    - keychain empty / corrupt → ``{status: "needs_oauth", action,
      reason}``. ``KeyringTokenStore.load`` collapses
      missing-slot, corrupt-JSON, and validation-failure into a
      single ``None`` return — all three map to the same operator
      action ("re-run ``auth login``").
    - token expired or near-expiry → ``{status: "needs_oauth",
      action, reason}`` with distinct text so the LLM can frame
      "your token expired" differently from "no token yet".
      Auto-refresh on 401 is a separate backlog item (M15.3
      follow-up); here we keep the surface explicit.
    - valid token → ``{status: "ready_for_profile_qa", reason}``.
      Placeholder until slice 2 fills the Q&A flow under the same
      status name.

    The ``settings`` argument is unused in slice 1 — kept on the
    factory signature so the slice 2 handler (which will need
    ``settings.audit_log_path.parent`` for the BusinessProfile
    JSONL) doesn't change the registration site.

    Why an MCP tool returns "run this CLI command" rather than
    triggering the OAuth flow itself: an MCP server cannot legally
    open a browser on the operator's machine — it runs as a
    background subprocess of Claude Desktop, with no UI ownership.
    The ``yadirect-agent auth login`` CLI command, by contrast,
    runs in the operator's terminal and OWNS the browser-launch
    decision. Returning an actionable next-step keeps the chat
    flow honest: the LLM tells the operator "please run X", and
    the operator stays in control.
    """

    del settings  # slice 1 has no settings dependencies

    async def handler(_raw: BaseModel, ctx: ToolContext) -> Any:
        token = KeyringTokenStore().load()
        if token is None:
            ctx.logger.info("tool.start_onboarding.needs_oauth_empty")
            return {
                "status": "needs_oauth",
                "action": "yadirect-agent auth login",
                "reason": (
                    "No OAuth token found in the OS keychain. "
                    "Run `yadirect-agent auth login` in your "
                    "terminal to grant the agent access to your "
                    "Yandex.Direct account."
                ),
            }
        if token.needs_refresh():
            ctx.logger.info("tool.start_onboarding.needs_oauth_expired")
            return {
                "status": "needs_oauth",
                "action": "yadirect-agent auth login",
                "reason": (
                    "Stored OAuth token is expired or near expiry. "
                    "Re-run `yadirect-agent auth login` in your "
                    "terminal to obtain a fresh token."
                ),
            }
        ctx.logger.info("tool.start_onboarding.ready")
        return {
            "status": "ready_for_profile_qa",
            "reason": (
                "OAuth token is valid. Next step: collect the "
                "BusinessProfile (niche, ICP, budget, goals, "
                "forbidden phrasings). Slice 2 will surface the "
                "Q&A flow here."
            ),
        }

    return Tool(
        name="start_onboarding",
        description=(
            "Conversational onboarding entry point. Use this when the user "
            "says 'help me set up the agent', 'how do I get started?', "
            "'configure me', or any equivalent in Russian (the operator "
            "speaks Russian; common phrasings include 'pomogi nastroit "
            "agenta', 'kak nachat?', 'nastroi menya'). The tool probes "
            "setup state and returns a structured next-step. Slice 1 only "
            "checks OAuth: returns {status: 'needs_oauth', action: "
            "'yadirect-agent auth login', reason: ...} when the OS "
            "keychain has no valid Yandex token (operator must run the "
            "CLI command — an MCP server cannot open a browser on the "
            "operator's machine), or {status: 'ready_for_profile_qa', "
            "reason: ...} when a valid token exists. Re-runnable: calling "
            "it again is always safe and idempotent."
        ),
        input_model=_StartOnboardingInput,
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
    _make_explain_decision_tool,
    _make_account_health_tool,
    _make_start_onboarding_tool,
]


def build_default_registry(settings: Settings) -> ToolRegistry:
    """Registry with the standard tool set bound to a Settings instance.

    The set grows over time as new milestones add tools; the
    enumeration lives in ``_GATED_FACTORIES`` and ``_PLAIN_FACTORIES``
    above so this docstring doesn't go stale every release.

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
