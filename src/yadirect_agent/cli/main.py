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
from rich.markup import escape as _rich_escape
from rich.table import Table

from .. import __version__
from ..agent.executor import (
    InvalidPlanStateError,
    PlanRejected,
    apply_plan,
)
from ..agent.loop import Agent, AgentLoopError, AgentRun
from ..agent.plans import OperationPlan, PendingPlansStore
from ..agent.tools import build_default_registry, build_safety_pair
from ..config import Settings, get_settings
from ..logging import configure_logging
from ..services.campaigns import CampaignService, CampaignSummary
from .doctor import (
    CheckResult,
    check_anthropic,
    check_direct_sandbox,
    check_env,
    check_policy_file,
)

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
# `doctor` — environment diagnostics.
# --------------------------------------------------------------------------


@app.command("doctor")
def doctor_cmd() -> None:
    """Probe env + Anthropic + Direct sandbox + policy file, report per-check status.

    Exit code: 0 if all checks are ok/warn, 2 if any check fails.
    Safe to run from cron as a liveness probe.
    """
    settings = _bootstrap_settings()
    results = asyncio.run(_run_doctor_checks(settings))
    _render_doctor_results(results)

    any_failed = any(r.status == "fail" for r in results)
    if any_failed:
        raise typer.Exit(code=2)


async def _run_doctor_checks(settings: Settings) -> list[CheckResult]:
    """Orchestrator. Serial on purpose — each check's output shapes the
    operator's next action, and a failed env check makes the rest
    irrelevant anyway."""
    return [
        await check_env(settings),
        check_policy_file(settings),
        await check_anthropic(settings),
        await check_direct_sandbox(settings),
    ]


def _render_doctor_results(results: list[CheckResult]) -> None:
    status_colour = {"ok": "green", "warn": "yellow", "fail": "red"}
    table = Table(show_header=True, header_style="bold")
    table.add_column("check")
    table.add_column("status")
    table.add_column("detail")
    for r in results:
        colour = status_colour.get(r.status, "white")
        table.add_row(r.name, f"[{colour}]{r.status}[/{colour}]", r.detail)
    _out.print(table)


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


# --------------------------------------------------------------------------
# `plans` — list / show pending operation plans (M2.2 data layer).
# --------------------------------------------------------------------------


plans_app = typer.Typer(
    name="plans",
    help="Inspect pending operation plans produced by the safety pipeline.",
    no_args_is_help=True,
)
app.add_typer(plans_app, name="plans")


def _plans_store(settings: Settings) -> PendingPlansStore:
    """Standard location for the plans JSONL: next to the audit log."""
    path = settings.audit_log_path.parent / "pending_plans.jsonl"
    return PendingPlansStore(path)


@plans_app.command("list")
def plans_list_cmd(
    show_all: Annotated[
        bool,
        typer.Option("--all", help="Include non-pending plans in the output."),
    ] = False,
) -> None:
    """Print pending operation plans (or all plans with --all)."""
    settings = _bootstrap_settings()
    store = _plans_store(settings)
    plans = store.all_plans() if show_all else store.list_pending()
    _render_plans_table(plans)


@plans_app.command("show")
def plans_show_cmd(
    plan_id: Annotated[str, typer.Argument(help="plan_id from `plans list`.")],
) -> None:
    """Print the full record for one plan."""
    settings = _bootstrap_settings()
    store = _plans_store(settings)
    plan = store.get(plan_id)
    if plan is None:
        _err.print(f"[red]no plan with id {plan_id!r}[/red]")
        raise typer.Exit(code=1)
    _render_plan_detail(plan)


def _render_plans_table(plans: list[OperationPlan]) -> None:
    if not plans:
        _out.print("[dim]no plans[/dim]")
        return
    table = Table(show_header=True, header_style="bold")
    table.add_column("plan_id")
    table.add_column("created")
    table.add_column("status")
    table.add_column("action")
    table.add_column("preview")
    status_colour = {
        "pending": "yellow",
        "approved": "cyan",
        "rejected": "red",
        "applied": "green",
    }
    for p in plans:
        colour = status_colour.get(p.status, "white")
        table.add_row(
            p.plan_id,
            p.created_at.isoformat(timespec="seconds"),
            f"[{colour}]{p.status}[/{colour}]",
            p.action,
            p.preview,
        )
    _out.print(table)


def _render_plan_detail(plan: OperationPlan) -> None:
    _out.print(f"[bold]plan_id[/bold]      {plan.plan_id}")
    _out.print(f"[bold]created_at[/bold]   {plan.created_at.isoformat()}")
    _out.print(f"[bold]action[/bold]       {plan.action}")
    _out.print(f"[bold]resource[/bold]     {plan.resource_type} {plan.resource_ids}")
    _out.print(f"[bold]status[/bold]       {plan.status}")
    if plan.status_updated_at is not None:
        _out.print(f"[bold]updated_at[/bold]   {plan.status_updated_at.isoformat()}")
    if plan.trace_id is not None:
        _out.print(f"[bold]trace_id[/bold]     {plan.trace_id}")
    _out.print(f"[bold]reason[/bold]       {plan.reason}")
    _out.print(f"[bold]preview[/bold]      {plan.preview}")
    if plan.args:
        _out.print("[bold]args:[/bold]")
        _out.print(_compact_json(plan.args))


# --------------------------------------------------------------------------
# `apply-plan` — operator approval for a pending OperationPlan (M2.2 part 3b2).
#
# The agent's ``set_campaign_budget`` tool returns ``status="pending"`` +
# ``plan_id`` when the safety pipeline asks for confirmation. The operator
# inspects via ``plans show <id>`` and runs ``apply-plan <id>`` to actually
# send the API request. ``apply_plan`` (in agent/executor.py) does:
#   1. validate the plan is in ``pending`` state with a stored review_context;
#   2. RE-REVIEW against the original snapshot (catches snapshot drift);
#   3. route through ``service_router(action, args, _applying_plan_id=...)``
#      which dispatches to the matching service method;
#   4. on success: ``store.update_status(applied)`` then ``on_applied`` (best-
#      effort); on executor failure: ``store.update_status(failed)`` and
#      propagate.
#
# Exit codes are designed for cron / shell-script consumption:
#   0  applied successfully
#   1  preconditions failed (unknown plan_id, not pending, no review_context)
#   2  re-review rejected the plan
#   3  the underlying service call raised
# --------------------------------------------------------------------------


# Service-router: a single-process registry mapping ``OperationPlan.action``
# strings to the service method that should execute them. Adding a new
# decorated method means adding one entry here. Deliberately a function
# (not a class) so the closure binds the shared safety pair from
# ``build_safety_pair`` exactly once per CLI invocation.
def _build_service_router(
    settings: Settings,
    pipeline: Any,
    store: PendingPlansStore,
    audit_sink: Any,
) -> Any:
    """Return an async callable ``(action, args, *, _applying_plan_id) -> Any``
    that ``apply_plan`` will dispatch through. Every service is constructed
    with the same ``(pipeline, store, audit_sink)`` triple so ``apply-plan``
    emits the same ``set_campaign_budget.requested|.ok|.failed`` audit
    line the agent path produces — distinguishable only by the
    ``actor`` field (``human`` vs ``agent``).
    """

    async def router(
        action: str,
        args: dict[str, Any],
        *,
        _applying_plan_id: str,
    ) -> Any:
        if action == "set_campaign_budget":
            svc = CampaignService(
                settings,
                pipeline=pipeline,
                store=store,
                audit_sink=audit_sink,
            )
            return await svc.set_daily_budget(
                **args,
                _applying_plan_id=_applying_plan_id,
            )
        msg = f"unknown action: {action!r}"
        raise ValueError(msg)

    return router


@app.command("apply-plan")
def apply_plan_cmd(
    plan_id: Annotated[
        str,
        typer.Argument(help="plan_id from `plans list`."),
    ],
) -> None:
    """Re-review and apply a pending OperationPlan against the live API.

    Exit codes (cron-friendly):
      0  applied successfully
      1  preconditions failed (unknown plan_id, not in `pending` state,
         or stored review_context is missing — see `plans show <id>`)
      2  re-review by the safety pipeline rejected the plan
      3  the underlying service call raised
    """
    settings = _bootstrap_settings()
    store = _plans_store(settings)
    pipeline, _, audit_sink = build_safety_pair(settings)
    # Reuse the store path from CLI convention rather than the one
    # build_safety_pair returned: `_plans_store` already encodes the
    # contract (next to audit log) and that's what tests / `plans list`
    # / agent runs all read. The audit_sink IS reused from the helper —
    # CLI-driven apply emits ``apply_plan.requested`` / ``.ok`` / ``.failed``
    # into the same JSONL the agent's tools registry writes to.
    router = _build_service_router(settings, pipeline, store, audit_sink)

    try:
        asyncio.run(
            apply_plan(
                plan_id,
                store=store,
                pipeline=pipeline,
                service_router=router,
                audit_sink=audit_sink,
            )
        )
    # Every interpolated value below is routed through ``_rich_escape`` so a
    # plan_id / exc.reason carrying rich-markup metacharacters (``[bold]`` …)
    # cannot manipulate the operator's terminal output. Auditor PR-B2 MEDIUM.
    except KeyError:
        _err.print(f"[red]plan not found: {plan_id!r}[/red]")
        raise typer.Exit(code=1) from None
    except InvalidPlanStateError as exc:
        # Plan exists but is not in ``pending`` (already applied / rejected /
        # failed) or has no stored review_context.
        _err.print(f"[red]{_rich_escape(str(exc))}[/red]")
        raise typer.Exit(code=1) from exc
    except PlanRejected as exc:
        _err.print(f"[red]rejected by re-review:[/red] {_rich_escape(exc.reason)}")
        for r in exc.blocking:
            _err.print(
                f"  - [yellow]{_rich_escape(r.status)}[/yellow] {_rich_escape(r.reason or '')}"
            )
        raise typer.Exit(code=2) from exc
    except Exception as exc:
        # Plan has already been moved to ``failed`` inside apply_plan
        # before the exception escapes — no double-apply risk for one
        # process. NB: concurrent apply-plan invocations on the same
        # plan_id are NOT protected against (no fcntl.flock); the JSONL
        # store assumes single-operator local use today. Tracked in
        # docs/BACKLOG.md "apply-plan concurrency / file-lock". Auditor
        # PR-B2 MEDIUM.
        _err.print(f"[red]apply failed:[/red] {type(exc).__name__}: {_rich_escape(str(exc))}")
        raise typer.Exit(code=3) from exc

    _out.print(f"[green]applied[/green] plan {_rich_escape(plan_id)}")


if __name__ == "__main__":  # pragma: no cover
    app()
