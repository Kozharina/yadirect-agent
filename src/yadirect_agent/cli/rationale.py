"""Renderer for ``yadirect-agent rationale`` (M20.3 slice).

Pure render layer for ``Rationale`` records: detail (``show``) and
list (``list``). Same separation as ``cli/health.py`` — keeping
formatting code out of ``main.py`` so both surfaces can grow
without bloating the typer wiring file.

Why all output strings escape Rich markup: ``summary``, ``message``-
like fields (``Alternative.description`` and friends) are
operator-set free text that flows from Direct/Metrika/business
profile data. Same hardening as ``cli/health.py`` HIGH-1 fix.
"""

from __future__ import annotations

import json

from rich.console import Console
from rich.markup import escape as _rich_escape
from rich.table import Table

from ..models.rationale import Rationale


def render_show_text(console: Console, rationale: Rationale) -> None:
    """Pretty-print one Rationale record to stdout."""
    console.print(f"[bold]decision_id[/bold]    {_rich_escape(rationale.decision_id)}")
    console.print(f"[bold]timestamp[/bold]      {rationale.timestamp.isoformat()}")
    console.print(f"[bold]action[/bold]         {_rich_escape(rationale.action)}")
    console.print(f"[bold]resource_type[/bold]  {_rich_escape(rationale.resource_type)}")
    if rationale.resource_ids:
        ids_str = ", ".join(str(i) for i in rationale.resource_ids)
        console.print(f"[bold]resource_ids[/bold]   {ids_str}")
    console.print(f"[bold]confidence[/bold]     {rationale.confidence.value}")
    console.print()
    console.print(f"[bold]summary[/bold]: {_rich_escape(rationale.summary)}")

    if rationale.inputs:
        console.print()
        console.print("[bold]inputs[/bold]:")
        for d in rationale.inputs:
            console.print(
                f"  • {_rich_escape(d.name)} = "
                f"{_rich_escape(str(d.value))} "
                f"(source={_rich_escape(d.source)}, "
                f"observed_at={d.observed_at.isoformat()})",
            )

    if rationale.alternatives_considered:
        console.print()
        console.print("[bold]alternatives considered[/bold]:")
        for alt in rationale.alternatives_considered:
            console.print(f"  • [yellow]{_rich_escape(alt.description)}[/yellow]")
            console.print(f"    rejected: {_rich_escape(alt.rejected_because)}")

    if rationale.policy_slack:
        console.print()
        console.print("[bold]policy slack[/bold]:")
        for check_name, slack in rationale.policy_slack.items():
            console.print(
                f"  • {_rich_escape(check_name)}: {slack}",
            )


def render_show_json(rationale: Rationale) -> str:
    """JSON payload for ``rationale show --json``.

    Round-trippable through ``Rationale.model_validate_json``; the
    confidence enum is serialised as its string value (consistent
    with the CLI text output and with ``cli/health.py`` JSON shape).
    """
    payload = json.loads(rationale.model_dump_json())
    return json.dumps(payload, ensure_ascii=False)


def render_list_text(console: Console, rationales: list[Rationale]) -> None:
    """Tabular listing for ``rationale list``."""
    if not rationales:
        console.print("[dim]no rationales[/dim]")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("decision_id")
    table.add_column("timestamp")
    table.add_column("action")
    table.add_column("resources")
    table.add_column("confidence")
    table.add_column("summary")

    for r in rationales:
        ids_str = ", ".join(str(i) for i in r.resource_ids) if r.resource_ids else "(account)"
        # All free-text columns escaped — operator-set strings travel
        # here verbatim from Direct/Metrika.
        table.add_row(
            _rich_escape(r.decision_id),
            r.timestamp.isoformat(timespec="seconds"),
            _rich_escape(r.action),
            ids_str,
            r.confidence.value,
            _rich_escape(r.summary),
        )
    console.print(table)
