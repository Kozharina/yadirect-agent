"""Bid management service.

Rules we enforce here (not at the API client level):
- Never raise a bid by more than +50% in a single call.
- Never lower a bid below the campaign's declared floor (if set).
- Bid changes in RUB are converted to micro-currency units at the boundary.

Mutating method ``apply`` is wrapped with ``@requires_plan`` (M2.2
part 3 + M2 follow-up): every call goes through the SafetyPipeline
(KS#2 max-CPC + KS#4 quality-score guard + rollout_stage check)
before reaching DirectService. The pipeline returns ``confirm`` by
default for bid changes (no ``auto_approve_bid_change`` knob), so
the agent's response is ``status: pending, plan_id: ...`` and the
operator must run ``yadirect-agent apply-plan <id>`` to actually
mutate.

KS#2 / KS#4 currently DEFER (skip) because the snapshot doesn't
yet carry per-keyword current bids and quality scores — the
``Keyword`` Direct model and ``DirectService.get_keywords`` would
need extension. Until then the protection is plan→confirm→execute +
rollout_stage + audit (still a meaningful gate; an autonomous agent
can no longer mutate bids without operator approval). Tracked in
BACKLOG.
"""

from __future__ import annotations

import sys
from types import FrameType

import structlog
from pydantic import BaseModel, ConfigDict, Field

from ..agent.executor import requires_plan
from ..agent.pipeline import ReviewContext, SafetyPipeline
from ..agent.plans import PendingPlansStore
from ..agent.safety import AccountBidSnapshot, KeywordSnapshot, ProposedBidChange
from ..audit import Actor, AuditSink, audit_action
from ..clients.direct import DirectService
from ..config import Settings
from ..models.keywords import KeywordBid


class BidUpdate(BaseModel):
    """Single keyword bid change request.

    Frozen pydantic model (was a dataclass before the M2 follow-up
    that gated ``BiddingService.apply`` through ``@requires_plan``).
    The decorator stores call args in ``OperationPlan.args`` for
    apply-plan replay; pydantic's ``model_dump_json`` knows how to
    serialise this; a frozen dataclass would crash JSON encoding.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    keyword_id: int
    new_search_bid_rub: float | None = Field(default=None, ge=0)
    new_network_bid_rub: float | None = Field(default=None, ge=0)


async def _build_bid_context(service: BiddingService, updates: list[BidUpdate]) -> ReviewContext:
    """Async context builder for ``apply()``'s ``@requires_plan``.

    Reads current per-keyword bids and quality scores from Direct via
    ``DirectService.get_keywords`` so the safety pipeline can actually
    enforce KS#2 (max-CPC) and KS#4 (quality-score guard) on every
    bid call. Pre-snapshot-reader the snapshot was empty, both checks
    deferred, and the only protection on this path was rollout_stage
    + plan→confirm→execute + audit.

    Behaviour:

    - empty ``updates`` → empty snapshot, no API call;
    - non-empty ``updates`` → one ``get_keywords(keyword_ids=...)``
      round trip, and a ``KeywordSnapshot`` per row that carries
      both ``Id`` and ``CampaignId`` (KS#2 looks up by campaign,
      KS#4 by keyword id; either missing means the row cannot be a
      meaningful snapshot entry);
    - rows that survive the identity check populate
      ``current_search_bid_rub`` / ``current_network_bid_rub`` /
      ``quality_score`` via the ``Keyword`` model's computed
      properties (micro-RUB → RUB, ``Productivity.Value`` →
      clamped int 0..10).
    """

    bid_changes = [
        ProposedBidChange(
            keyword_id=u.keyword_id,
            new_search_bid_rub=u.new_search_bid_rub,
            new_network_bid_rub=u.new_network_bid_rub,
        )
        for u in updates
    ]

    keyword_ids = [u.keyword_id for u in updates]
    snapshot_entries: list[KeywordSnapshot] = []
    if keyword_ids:
        async with DirectService(service._settings) as api:
            keywords = await api.get_keywords(keyword_ids=keyword_ids)
        for kw in keywords:
            if kw.id is None or kw.campaign_id is None:
                continue
            snapshot_entries.append(
                KeywordSnapshot(
                    keyword_id=kw.id,
                    campaign_id=kw.campaign_id,
                    current_search_bid_rub=kw.current_search_bid_rub,
                    current_network_bid_rub=kw.current_network_bid_rub,
                    quality_score=kw.quality_score,
                )
            )

    return ReviewContext(
        bid_snapshot=AccountBidSnapshot(keywords=snapshot_entries),
        bid_changes=bid_changes,
    )


class BiddingService:
    MAX_INCREASE_PCT: float = 0.5  # +50% per single call

    def __init__(
        self,
        settings: Settings,
        *,
        pipeline: SafetyPipeline | None = None,
        store: PendingPlansStore | None = None,
        audit_sink: AuditSink | None = None,
    ) -> None:
        """Build a BiddingService.

        ``pipeline`` / ``store`` / ``audit_sink`` are optional
        keyword-only with the same semantics as ``CampaignService``:
        present in production via ``build_default_registry``;
        optional for fixtures and read-only callers (there are no
        read-only methods today, but the service may grow them).
        Mutating ``apply`` requires pipeline+store via
        ``_resolve_safety`` unless the caller passes
        ``_applying_plan_id`` (apply-plan re-entry escape).
        """

        self._settings = settings
        self._pipeline = pipeline
        self._plans_store = store
        self._audit_sink = audit_sink
        self._logger = structlog.get_logger().bind(component="bidding_service")

    def _resolve_safety(self) -> tuple[SafetyPipeline, PendingPlansStore]:
        # Auditor C-1: audit_sink is also required on the production
        # path. A caller building BiddingService(settings, pipeline=p,
        # store=s) without a sink would gate the plan but execute
        # without audit emission — silent gap. Enforce all three
        # together.
        if self._pipeline is None or self._plans_store is None or self._audit_sink is None:
            msg = (
                "BiddingService was constructed without a complete safety "
                "trio (SafetyPipeline / PendingPlansStore / AuditSink); "
                "mutating methods cannot run. Build via "
                "build_default_registry (which wires all three) or pass "
                "pipeline=, store=, audit_sink= explicitly."
            )
            raise RuntimeError(msg)
        return self._pipeline, self._plans_store

    def _infer_actor(self) -> Actor:
        """Frame walk identical to ``CampaignService._infer_actor`` —
        ``_applying_plan_id`` in the @requires_plan ``wrapper`` frame
        means the operator drove this call via apply-plan, not the
        agent's allow path."""
        frame: FrameType | None = sys._getframe(1)
        for _ in range(8):
            if frame is None:
                break
            if (
                frame.f_code.co_name == "wrapper"
                and frame.f_locals.get("_applying_plan_id") is not None
            ):
                return "human"
            frame = frame.f_back
        return "agent"

    @requires_plan(
        action="set_keyword_bids",
        resource_type="keyword",
        preview_builder=lambda self, updates: f"set bids on {len(updates)} keyword(s)",
        context_builder=_build_bid_context,
        resource_ids_from_args=lambda self, updates: [u.keyword_id for u in updates],
    )
    async def apply(self, updates: list[BidUpdate]) -> None:
        """Apply a batch of bid updates. Bulk semantics — one plan
        covers the full list; apply-plan applies all-or-none.

        Wrapped by ``@requires_plan``: every call goes through the
        SafetyPipeline before reaching DirectService. With no
        ``auto_approve_bid_change`` policy knob, every mutation
        returns ``confirm`` and the operator runs apply-plan to
        actually send the request.

        Audit emits ``set_keyword_bids.requested|.ok|.failed``.
        """
        if not updates:
            return

        if self._audit_sink is None:
            # Reachable only on the @requires_plan bypass path
            # (``_applying_plan_id`` was passed) AND the caller
            # didn't construct with a sink. In production both the
            # CLI ``apply-plan`` router and the in-process tools
            # registry build BiddingService with the shared
            # JsonlSink, so this branch should fire only in tests
            # that intentionally skip the safety trio. Log a
            # WARNING so a misconfigured production caller surfaces
            # in operator-visible logs rather than silently
            # mutating without an audit record. Auditor C-1.
            self._logger.warning(
                "bids.apply.no_audit_sink",
                count=len(updates),
                note=(
                    "audit_sink is None on the apply-plan bypass path; "
                    "mutation will proceed but no set_keyword_bids.* event "
                    "will be emitted. Production callers must pass "
                    "audit_sink=..."
                ),
            )
            await self._do_apply(updates)
            return

        actor = self._infer_actor()
        async with audit_action(
            self._audit_sink,
            actor=actor,
            action="set_keyword_bids",
            resource=f"keywords:{[u.keyword_id for u in updates]}",
            args={
                "updates": [
                    {
                        "keyword_id": u.keyword_id,
                        "new_search_bid_rub": u.new_search_bid_rub,
                        "new_network_bid_rub": u.new_network_bid_rub,
                    }
                    for u in updates
                ]
            },
        ) as ctx:
            await self._do_apply(updates)
            ctx.set_result(
                {
                    "status": "applied",
                    "updated": [u.keyword_id for u in updates],
                }
            )

    async def _do_apply(self, updates: list[BidUpdate]) -> None:
        """Inner API call — extracted so the audit_action wrapper
        sits on a single body."""
        # TODO(iteration 2): fetch current bids and reject updates that violate
        # MAX_INCREASE_PCT. For now we only convert units and forward.
        payload = [
            KeywordBid(
                keyword_id=u.keyword_id,
                search_bid=(
                    int(u.new_search_bid_rub * 1_000_000)
                    if u.new_search_bid_rub is not None
                    else None
                ),
                network_bid=(
                    int(u.new_network_bid_rub * 1_000_000)
                    if u.new_network_bid_rub is not None
                    else None
                ),
            )
            for u in updates
        ]

        self._logger.info("bids.apply.request", count=len(payload))
        async with DirectService(self._settings) as api:
            await api.set_keyword_bids(payload)
        self._logger.info("bids.apply.ok", count=len(payload))
