"""`yadirect-agent` CLI — typer wrapper over the agent core.

Commands
--------
- `run "<task>"` — one-shot agent execution. Prints the agent's final text,
  a compact tool-call trace, and token totals. Suitable for cron.
- `chat` — interactive REPL. Each line is a fresh `run()` for now; we'll add
  conversation continuity once M2 audit is in (context compression is a
  later concern).
- `list-campaigns` — bypass the model, call the service directly. Useful
  for smoke-testing credentials and sandbox visibility.
- `--version` — print `yadirect_agent.__version__` and exit.

Design notes
------------
- typer does not natively call async functions. We wrap each command with
  `asyncio.run(...)`; this is the standard workaround and keeps the command
  functions obviously sync at the typer boundary.
- Output uses `rich.console.Console` so logs (stderr, structlog JSON) and
  user-facing output (stdout) stay separable. Piping `... | jq` works.
- Errors are formatted to stderr; exit code is non-zero. No traceback is
  shown unless `--debug` is set — the intended audience is ops staff, not
  library developers.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from typing import Annotated, Any

import structlog
import typer
from rich.console import Console
from rich.table import Table

from .. import __version__
from ..agent.loop import Agent, AgentLoopError, AgentRun
from ..agent.tools import build_default_registry
from ..config import Settings, get_settings
from ..logging import configure_logging
from ..services.campaigns import CampaignService, CampaignSummary

app = typer.Typer(
    name="yadirect-agent",
    help="Autonomous agent for Yandex.Direct account management.",
    no_args_is_help=True,
    add_completion=False,
)

_out = Console()
_err = Console(stderr=True)


# --------------------------------------------------------------------------
# Root callback — handles --version and global flags.
# --------------------------------------------------------------------------


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"yadirect-agent {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show version and exit.",
        ),
    ] = False,
) -> None:
    """Root callback; typer requires at least one."""


# --------------------------------------------------------------------------
# `run` — one-shot agent task.
# --------------------------------------------------------------------------


@app.command("run")
def run_cmd(
    task: Annotated[str, typer.Argument(help="Task description in plain text.")],
) -> None:
    """Run an ad-hoc agent task and print the outcome."""
    settings = _bootstrap_settings()
    try:
        result = asyncio.run(_run_agent(settings, task))
    except AgentLoopError as exc:
        _err.print(f"[red]agent aborted:[/red] {exc}")
        raise typer.Exit(code=2) from exc
    _render_agent_run(result)


# --------------------------------------------------------------------------
# `chat` — interactive REPL (one-shot turns for now).
# --------------------------------------------------------------------------


@app.command("chat")
def chat_cmd() -> None:
    """Interactive loop — each line becomes an independent agent run."""
    settings = _bootstrap_settings()
    _out.print("[dim]yadirect-agent chat. Type an empty line or Ctrl-D to exit.[/dim]")
    while True:
        try:
            task = typer.prompt("you", default="", show_default=False)
        except (EOFError, KeyboardInterrupt):
            _out.print()
            return
        if not task.strip():
            return
        try:
            result = asyncio.run(_run_agent(settings, task))
        except AgentLoopError as exc:
            _err.print(f"[red]agent aborted:[/red] {exc}")
            continue
        _render_agent_run(result)


# --------------------------------------------------------------------------
# `list-campaigns` — bypass the model.
# --------------------------------------------------------------------------


@app.command("list-campaigns")
def list_campaigns_cmd(
    state: Annotated[
        str | None,
        typer.Option(
            "--state",
            help="ON | OFF | SUSPENDED | ENDED | CONVERTED | ARCHIVED (repeatable).",
        ),
    ] = None,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Emit JSON to stdout instead of a table."),
    ] = False,
) -> None:
    """Direct pass-through to CampaignService — no LLM involved."""
    settings = _bootstrap_settings()
    service = CampaignService(settings)

    async def fetch() -> list[CampaignSummary]:
        if state is None:
            return await service.list_all()
        return await service.list_active()

    summaries = asyncio.run(fetch())
    if as_json:
        typer.echo(json.dumps([asdict(s) for s in summaries], ensure_ascii=False))
        return
    _render_campaigns_table(summaries)


# --------------------------------------------------------------------------
# Internals.
# --------------------------------------------------------------------------


def _bootstrap_settings() -> Settings:
    settings = get_settings()
    configure_logging(settings)
    structlog.contextvars.clear_contextvars()
    return settings


async def _run_agent(settings: Settings, task: str) -> AgentRun:
    registry = build_default_registry(settings)
    agent = Agent(settings, registry)
    return await agent.run(task)


def _render_agent_run(result: AgentRun) -> None:
    _out.print()
    if result.final_text:
        _out.print(result.final_text)
    _out.print()
    _out.rule("[dim]tool calls[/dim]")
    if not result.tool_calls:
        _out.print("[dim](no tool calls)[/dim]")
    else:
        for i, call in enumerate(result.tool_calls, start=1):
            status = "[green]ok[/green]" if call.ok else "[red]err[/red]"
            _out.print(
                f"[dim]{i:>2}.[/dim] {call.name} {status} "
                f"[dim]args={_compact_json(call.input)}[/dim]"
            )
            if not call.ok:
                _out.print(f"     [red]{call.error}[/red]")
    _out.rule("[dim]usage[/dim]")
    _out.print(
        f"iterations={result.iterations}  "
        f"input_tokens={result.input_tokens}  "
        f"output_tokens={result.output_tokens}  "
        f"stop_reason={result.stop_reason}  "
        f"trace_id={result.trace_id}"
    )


def _render_campaigns_table(summaries: list[CampaignSummary]) -> None:
    if not summaries:
        _out.print("[dim]no campaigns[/dim]")
        return
    table = Table(show_header=True, header_style="bold")
    table.add_column("id", justify="right")
    table.add_column("name")
    table.add_column("state")
    table.add_column("status")
    table.add_column("type")
    table.add_column("budget, RUB", justify="right")
    for s in summaries:
        table.add_row(
            str(s.id),
            s.name,
            s.state,
            s.status,
            s.type or "",
            "—" if s.daily_budget_rub is None else f"{s.daily_budget_rub:g}",
        )
    _out.print(table)


def _compact_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return repr(value)


if __name__ == "__main__":  # pragma: no cover
    app()
