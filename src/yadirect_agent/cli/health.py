"""Renderer for ``yadirect-agent health`` (M15.5.1).

Pure render layer — takes a ``HealthReport``, produces stdout. No
network, no service calls. The CLI command in ``main.py`` orchestrates
the run; this file owns "how does it look on the operator's screen".

Why isolated: the render logic is the most likely surface to grow as
new rules land (severity colors, sorting, JSON mode, future Markdown
mode for M12 reports). Keeping it here means ``main.py`` stays focused
on argument parsing and lifecycle.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from rich.console import Console
from rich.table import Table

from ..models.health import Finding, HealthReport, Severity

# Severity ordering for rendering: HIGH first (most urgent), then
# warnings, then info — same order an operator scans for action items.
_SEVERITY_ORDER: dict[Severity, int] = {
    Severity.HIGH: 0,
    Severity.WARNING: 1,
    Severity.INFO: 2,
}

_SEVERITY_COLOUR: dict[Severity, str] = {
    Severity.HIGH: "red",
    Severity.WARNING: "yellow",
    Severity.INFO: "cyan",
}


def _sort_key(f: Finding) -> tuple[int, float, int]:
    """Sort findings by (severity desc, impact desc, campaign_id asc).

    Operator's natural reading order: most urgent first within a
    severity bucket, then by money at stake, then stable by id so
    re-runs produce the same ordering.
    """
    impact_neg = -(f.estimated_impact_rub or 0.0)
    return _SEVERITY_ORDER[f.severity], impact_neg, f.campaign_id or 0


def render_report_text(console: Console, report: HealthReport) -> None:
    """Pretty-print the report as a Rich table on stdout."""
    if not report.has_findings:
        console.print(
            "[green]No issues found[/green] over "
            f"{report.date_range.start.isoformat()} to "
            f"{report.date_range.end.isoformat()}.",
        )
        return

    sorted_findings = sorted(report.findings, key=_sort_key)

    table = Table(show_header=True, header_style="bold")
    table.add_column("severity")
    table.add_column("rule")
    table.add_column("campaign")
    table.add_column("impact (RUB)", justify="right")
    table.add_column("message")

    for f in sorted_findings:
        colour = _SEVERITY_COLOUR.get(f.severity, "white")
        impact = f"{f.estimated_impact_rub:.0f}" if f.estimated_impact_rub is not None else "—"
        campaign = (
            f"{f.campaign_name} (#{f.campaign_id})" if f.campaign_id is not None else "(account)"
        )
        table.add_row(
            f"[{colour}]{f.severity.value}[/{colour}]",
            f.rule_id,
            campaign,
            impact,
            f.message,
        )
    console.print(table)

    # Footer with totals so the operator sees aggregate impact at a
    # glance — useful when the table runs off-screen.
    total_impact = sum(f.estimated_impact_rub or 0.0 for f in report.findings)
    by_sev = {s: len(report.findings_by_severity(s)) for s in Severity}
    summary_parts = [f"{by_sev[s]} {s.value}" for s in Severity if by_sev[s] > 0]
    console.print(
        f"\n[bold]{len(report.findings)} findings[/bold] "
        f"({', '.join(summary_parts)}); "
        f"[bold]{total_impact:.0f} RUB[/bold] estimated impact",
    )


def render_report_json(report: HealthReport) -> str:
    """Serialise the report to JSON for downstream tools / piping."""
    payload: dict[str, Any] = {
        "date_range": {
            "start": report.date_range.start.isoformat(),
            "end": report.date_range.end.isoformat(),
        },
        "findings": [
            {
                **{k: v for k, v in asdict(f).items() if k != "severity"},
                "severity": f.severity.value,
            }
            for f in sorted(report.findings, key=_sort_key)
        ],
    }
    return json.dumps(payload, ensure_ascii=False)
