"""Renderer for ``yadirect-agent cost status`` (M21).

Pure render layer. Reads CostStore + Settings, formats a human-
readable summary covering the current calendar month plus the
previous month for trend context.

Why both months: a "spent X RUB this month" number alone is not
actionable mid-month — operators want to know whether they're
trending higher or lower than last month at the same cadence.
A two-month view answers that without needing a graph.

Operator-set strings (model names) flow through ``_rich_escape``
mirroring the M15.5.1 / M20 / M15.2 hardening pattern.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from rich.console import Console
from rich.markup import escape as _rich_escape
from rich.table import Table

from ..config import Settings
from ..models.cost import MonthlyCostSummary, aggregate_records


def _now_year_month() -> tuple[int, int]:
    now = datetime.now(UTC)
    return now.year, now.month


def _previous_month(year: int, month: int) -> tuple[int, int]:
    if month == 1:
        return year - 1, 12
    return year, month - 1


def _project_end_of_month(
    summary: MonthlyCostSummary,
    *,
    day_now: int,
    days_in_month: int,
) -> float:
    """Linear extrapolation of monthly spend.

    Naive but operator-readable: ``(spent_so_far / day_now) * days_in_month``.
    Operators understand "you're on track for X RUB this month" — anything
    fancier (weekly cycles, weekend dips) is not justified at this layer.
    """
    if day_now <= 0:
        return summary.total_cost_rub
    return summary.total_cost_rub * (days_in_month / day_now)


def render_status_text(
    console: Console,
    summaries_by_month: dict[tuple[int, int], MonthlyCostSummary],
    settings: Settings,
) -> None:
    """Pretty-print the cost summary as a Rich table on stdout."""
    year, month = _now_year_month()
    prev_year, prev_month = _previous_month(year, month)

    current = summaries_by_month.get((year, month))
    previous = summaries_by_month.get((prev_year, prev_month))

    if current is None and previous is None:
        console.print("[dim]No agent runs recorded yet — nothing to report.[/dim]")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("month")
    table.add_column("runs", justify="right")
    table.add_column("input tokens", justify="right")
    table.add_column("output tokens", justify="right")
    table.add_column("cost (RUB)", justify="right")

    def _fmt(s: MonthlyCostSummary | None, label: str) -> None:
        if s is None:
            table.add_row(label, "—", "—", "—", "—")
            return
        table.add_row(
            label,
            str(s.run_count),
            f"{s.total_input_tokens:,}",
            f"{s.total_output_tokens:,}",
            f"{s.total_cost_rub:.2f}",
        )

    _fmt(previous, f"{prev_year}-{prev_month:02d} (prev)")
    _fmt(current, f"{year}-{month:02d} (current)")

    console.print(table)

    # Projection + budget view for the current month.
    if current is not None:
        from calendar import monthrange

        _, days_in_month = monthrange(year, month)
        day_now = datetime.now(UTC).day
        projected = _project_end_of_month(
            current,
            day_now=day_now,
            days_in_month=days_in_month,
        )
        budget = settings.agent_monthly_llm_budget_rub
        spent = current.total_cost_rub

        console.print(
            f"\n[bold]Projected end-of-month spend:[/bold] "
            f"{projected:.2f} RUB "
            f"(based on {spent:.2f} RUB across {day_now} days)",
        )

        if budget is not None:
            pct_used = spent / budget * 100 if budget > 0 else 0
            pct_projected = projected / budget * 100 if budget > 0 else 0
            colour = "green" if pct_projected < 80 else "yellow" if pct_projected < 100 else "red"
            console.print(
                f"[bold]Budget:[/bold] {budget:.2f} RUB; "
                f"used {pct_used:.0f}%; "
                f"projected [{colour}]{pct_projected:.0f}%[/{colour}]",
            )
        else:
            console.print(
                "[dim]No monthly LLM budget configured "
                "(set AGENT_MONTHLY_LLM_BUDGET_RUB to see budget warnings).[/dim]",
            )

    # Pricing footer — operator can sanity-check what the cost was
    # computed against.
    console.print(
        f"\n[dim]Conversion: {settings.usd_to_rub_rate:.2f} RUB/USD "
        f"(set USD_TO_RUB_RATE to override). "
        f"Model: {_rich_escape(settings.anthropic_model)}.[/dim]",
    )


def render_status_json(
    summaries_by_month: dict[tuple[int, int], MonthlyCostSummary],
    settings: Settings,
) -> str:
    """JSON payload for ``cost status --json``."""
    year, month = _now_year_month()
    prev_year, prev_month = _previous_month(year, month)
    current = summaries_by_month.get((year, month))
    previous = summaries_by_month.get((prev_year, prev_month))

    payload: dict[str, Any] = {
        "current_month": json.loads(current.model_dump_json()) if current else None,
        "previous_month": json.loads(previous.model_dump_json()) if previous else None,
        "settings": {
            "usd_to_rub_rate": settings.usd_to_rub_rate,
            "monthly_budget_rub": settings.agent_monthly_llm_budget_rub,
            "model": settings.anthropic_model,
        },
    }
    return json.dumps(payload, ensure_ascii=False)


def aggregate_for_status(records: list[Any]) -> dict[tuple[int, int], MonthlyCostSummary]:
    """Convenience wrapper around aggregate_records for the CLI."""
    return aggregate_records(records)
