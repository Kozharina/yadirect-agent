"""LLM cost calculator + JSONL persistence (M21).

Two pieces tightly coupled:

- ``CostCalculator``: pure function from ``(model, tokens, settings)
  → CostRecord``. Reads ``Settings.anthropic_pricing`` (or a defaults
  fallback) and ``Settings.usd_to_rub_rate``, snapshots them onto the
  record so a later read knows exactly which prices applied.
- ``CostStore``: JSONL append-only sibling to audit / pending_plans /
  rationale stores. Same operational contract: tamper-evident,
  defensive parsing of corrupt lines, missing-file = empty reads.

The calculator emits a record per ``messages.create`` call. The
agent loop is responsible for invoking it and persisting; this
module is intentionally unopinionated about *when* records are
written — it just knows *how*.

Pricing snapshot rationale: a record from last month should reflect
last month's prices, not whatever Settings says today. We store
input_usd_per_million / output_usd_per_million / usd_to_rub_rate on
each record so re-reads stay accurate even after Anthropic changes
its pricing or the operator updates the conversion rate.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from ..models.cost import CostRecord, ModelPricing

if TYPE_CHECKING:
    from ..config import Settings

_log = structlog.get_logger(component="agent.cost")


# Anthropic's published pricing snapshot (as of 2026-04). Update when
# Anthropic changes rates. USD per million tokens.
#
# Source: https://docs.anthropic.com/en/docs/build-with-claude/pricing
# Cached input is 90% cheaper than fresh input per Anthropic's
# documentation; we round to the nearest cent in the table.
DEFAULT_ANTHROPIC_PRICING: dict[str, ModelPricing] = {
    "claude-opus-4-7": ModelPricing(
        model="claude-opus-4-7",
        input_usd_per_million=15.0,
        output_usd_per_million=75.0,
        cached_input_usd_per_million=1.5,
    ),
    "claude-sonnet-4-7": ModelPricing(
        model="claude-sonnet-4-7",
        input_usd_per_million=3.0,
        output_usd_per_million=15.0,
        cached_input_usd_per_million=0.3,
    ),
    "claude-haiku-4-5": ModelPricing(
        model="claude-haiku-4-5",
        input_usd_per_million=1.0,
        output_usd_per_million=5.0,
        cached_input_usd_per_million=0.1,
    ),
}


def _resolve_pricing(model: str) -> ModelPricing:
    """Look up pricing for ``model``; fall back to Opus rates if unknown.

    Conservative fallback: an unknown model is most likely a future
    Opus or near-Opus tier (Anthropic doesn't release cheaper models
    by surprise). Using Opus rates for an unknown model produces an
    overcount — better than an undercount that hides spend from
    cost status.
    """
    pricing = DEFAULT_ANTHROPIC_PRICING.get(model)
    if pricing is None:
        _log.warning(
            "cost.unknown_model_falls_back_to_opus",
            model=model,
            fallback="claude-opus-4-7",
        )
        return DEFAULT_ANTHROPIC_PRICING["claude-opus-4-7"]
    return pricing


def calculate_cost(
    *,
    trace_id: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int = 0,
    settings: Settings,
    timestamp: datetime | None = None,
) -> CostRecord:
    """Compute ``CostRecord`` from token counts + Settings.

    All conversion factors (price-per-million, USD-to-RUB rate)
    are snapshot onto the returned record so the persisted
    representation stays meaningful after Settings changes.

    The math is straightforward (USD/M tokens * tokens / 1e6 * rate)
    but centralised here so a future change (e.g. tiered pricing,
    volume discounts) lands in one place.
    """
    pricing = _resolve_pricing(model)
    rate = settings.usd_to_rub_rate

    # Per-million → per-token, then sum.
    input_usd = (input_tokens / 1_000_000) * pricing.input_usd_per_million
    output_usd = (output_tokens / 1_000_000) * pricing.output_usd_per_million
    cached_input_usd = (cached_input_tokens / 1_000_000) * pricing.cached_input_usd_per_million

    total_usd = input_usd + output_usd + cached_input_usd
    cost_rub = total_usd * rate

    return CostRecord(
        timestamp=timestamp or datetime.now(UTC),
        trace_id=trace_id,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=cached_input_tokens,
        input_usd_per_million=pricing.input_usd_per_million,
        output_usd_per_million=pricing.output_usd_per_million,
        cached_input_usd_per_million=pricing.cached_input_usd_per_million,
        usd_to_rub_rate=rate,
        cost_rub=cost_rub,
    )


class CostStore:
    """JSONL append-only store of ``CostRecord`` entries.

    Sibling to PendingPlansStore, RationaleStore, and the audit sink.
    Same defensive parsing of corrupt lines (skip with structlog
    warning) and same missing-file-is-normal semantics.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    # -- Writes --------------------------------------------------------------

    def append(self, record: CostRecord) -> None:
        """Append ``record`` as a JSON line. Creates parent dirs on demand."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as f:
            f.write(record.model_dump_json() + "\n")

    # -- Reads ---------------------------------------------------------------

    def all_records(self) -> list[CostRecord]:
        """Every record in chronological-insertion order, oldest first.

        Defensive parsing: corrupt lines are skipped and a single
        structlog warning is emitted per scan (not per skip — keeps
        logs readable when a whole file is botched). Returns an empty
        list when the file is missing.
        """
        if not self._path.exists():
            return []
        out: list[CostRecord] = []
        skipped = 0
        with self._path.open(encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    record = CostRecord.model_validate_json(line)
                except (json.JSONDecodeError, ValueError):
                    skipped += 1
                    continue
                out.append(record)
        if skipped > 0:
            _log.warning(
                "cost.store.corrupt_lines_skipped",
                path=str(self._path),
                skipped=skipped,
            )
        return out

    def records_in_month(self, *, year: int, month: int) -> list[CostRecord]:
        """Records whose timestamp falls in the given (year, month) bucket."""
        return [
            r for r in self.all_records() if r.timestamp.year == year and r.timestamp.month == month
        ]


__all__ = [
    "DEFAULT_ANTHROPIC_PRICING",
    "CostStore",
    "calculate_cost",
]
