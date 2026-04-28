"""LLM cost tracking models (M21).

The agent calls Anthropic's API on every ``run()``. Without per-call
cost capture, those costs accumulate invisibly — by the time the
operator notices the monthly bill it's already month-end. This
module is the data model: how we represent one priced call, how we
aggregate them, and how the pricing table itself is shaped.

Three frozen dataclasses:

- ``ModelPricing`` — Anthropic's published price for one model. USD
  per million tokens, separate input/output. Our pricing constants
  live in ``Settings.anthropic_pricing`` and update when Anthropic
  changes its rates (a manual update; no live lookup).
- ``CostRecord`` — one priced API call. The unit of persistence in
  ``logs/cost.jsonl``.
- ``MonthlyCostSummary`` — aggregation across all CostRecords in a
  given month. The unit of read-back for ``cost status``.

Why JSONL persistence (not SQLite or aggregation-only): same reasons
audit.jsonl uses JSONL — append-only, tamper-evident, defensive
parsing of corrupt lines, and easy to grep / jq for ad-hoc analysis.
A future ``services/reporting.py`` extension can compute richer
aggregates (top-N expensive runs, cost per tool, etc.) on top.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ModelPricing(BaseModel):
    """Anthropic's per-model pricing. USD per *million* tokens.

    The ``per_million`` convention matches Anthropic's published table
    (``https://docs.anthropic.com/en/docs/build-with-claude/pricing``).
    Update when prices change — this is intentionally not a live API
    lookup; manual maintenance every release cycle is the right
    cadence for a product-pricing dimension.
    """

    model_config = ConfigDict(extra="forbid")

    model: str = Field(..., min_length=1)
    input_usd_per_million: float = Field(..., ge=0)
    output_usd_per_million: float = Field(..., ge=0)
    # Cached prompts are 90% cheaper for cache hits per Anthropic's
    # docs. Default 0 = treat caching as input pricing if not set
    # (conservative undercount is safer than overcount: an agent
    # whose monthly bill is BIGGER than projected will surprise the
    # operator far more than one whose bill is smaller).
    cached_input_usd_per_million: float = Field(default=0.0, ge=0)


class CostRecord(BaseModel):
    """One priced ``messages.create`` call.

    Records the inputs (model + token counts) and the derived RUB
    amount. We persist the conversion factors at record time so a
    later ``cost status`` reading the file knows the exact pricing
    snapshot, not whatever ``Settings`` says today (rates and
    pricing both drift; a record from last month should reflect
    last month's prices).
    """

    model_config = ConfigDict(extra="ignore")

    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    trace_id: str = Field(..., min_length=1)
    """Aligned with ``AgentRun.trace_id`` — same identifier so a
    cost record can be correlated to its agent run."""

    model: str = Field(..., min_length=1)
    input_tokens: int = Field(..., ge=0)
    output_tokens: int = Field(..., ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)

    # Per-record pricing snapshot. Captured at write time so a file
    # entry stays meaningful even after Settings.anthropic_pricing or
    # Settings.usd_to_rub_rate are updated.
    input_usd_per_million: float = Field(..., ge=0)
    output_usd_per_million: float = Field(..., ge=0)
    cached_input_usd_per_million: float = Field(default=0.0, ge=0)
    usd_to_rub_rate: float = Field(..., gt=0)

    cost_rub: float = Field(..., ge=0)
    """Total RUB cost for this single call. Computed at construction
    by ``CostCalculator``; persisted as-is so re-reads are cheap."""

    @field_validator("trace_id")
    @classmethod
    def _no_whitespace_in_trace_id(cls, v: str) -> str:
        if any(ch.isspace() for ch in v):
            msg = "trace_id must not contain whitespace"
            raise ValueError(msg)
        return v

    @field_validator(
        "input_usd_per_million",
        "output_usd_per_million",
        "cached_input_usd_per_million",
        "usd_to_rub_rate",
        "cost_rub",
    )
    @classmethod
    def _finite_only(cls, v: float) -> float:
        # Reject IEEE-754 specials at construction (same hardening
        # as Settings.account_target_cpa_rub from M15.5.1 MEDIUM-2).
        # An ``inf`` or ``nan`` propagating into cost_rub would crash
        # ``json.dumps`` in the JSONL store and abort the agent run.
        import math

        if not math.isfinite(v):
            msg = f"value must be finite, got {v!r}"
            raise ValueError(msg)
        return v


class MonthlyCostSummary(BaseModel):
    """Aggregate of all CostRecords in one (year, month) window.

    What ``yadirect-agent cost status`` consumes. Carries enough
    detail to support both human-readable display ("you spent X
    rubles this month, Y rubles projected") and automated checks
    (a future cron alert when projected > budget).
    """

    model_config = ConfigDict(extra="forbid")

    year: int = Field(..., ge=2000, le=9999)
    month: int = Field(..., ge=1, le=12)
    total_input_tokens: int = Field(..., ge=0)
    total_output_tokens: int = Field(..., ge=0)
    total_cost_rub: float = Field(..., ge=0)
    run_count: int = Field(..., ge=0)
    """Number of distinct ``trace_id`` values aggregated. Useful
    because a single agent run can issue multiple ``messages.create``
    calls (one per tool-use turn); knowing the run count lets the
    operator see "5 runs averaged 200 RUB each" rather than
    "100 calls at 10 RUB each", which is more actionable."""


def aggregate_records(records: list[CostRecord]) -> dict[tuple[int, int], MonthlyCostSummary]:
    """Bucket CostRecords by (year, month) into MonthlyCostSummary.

    Returned dict is keyed ``(year, month)`` for stable lookup.
    A record with timestamp 2026-04-15 falls into bucket (2026, 4).
    """
    buckets: dict[tuple[int, int], list[CostRecord]] = {}
    for r in records:
        key = (r.timestamp.year, r.timestamp.month)
        buckets.setdefault(key, []).append(r)

    out: dict[tuple[int, int], MonthlyCostSummary] = {}
    for (year, month), bucket in buckets.items():
        trace_ids: set[str] = {r.trace_id for r in bucket}
        out[(year, month)] = MonthlyCostSummary(
            year=year,
            month=month,
            total_input_tokens=sum(r.input_tokens for r in bucket),
            total_output_tokens=sum(r.output_tokens for r in bucket),
            total_cost_rub=sum(r.cost_rub for r in bucket),
            run_count=len(trace_ids),
        )
    return out


__all__ = [
    "CostRecord",
    "ModelPricing",
    "MonthlyCostSummary",
    "aggregate_records",
]
