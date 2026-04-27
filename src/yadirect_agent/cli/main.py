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
from datetime import UTC, datetime
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
from ..audit import AuditEvent, AuditSink, audit_action
from ..config import Settings, get_settings
from ..logging import configure_logging
from ..rollout import RolloutState, RolloutStateStore
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
        svc = CampaignService(
            settings,
            pipeline=pipeline,
            store=store,
            audit_sink=audit_sink,
        )
        if action == "set_campaign_budget":
            return await svc.set_daily_budget(**args, _applying_plan_id=_applying_plan_id)
        if action == "pause_campaigns":
            return await svc.pause(args["campaign_ids"], _applying_plan_id=_applying_plan_id)
        if action == "resume_campaigns":
            return await svc.resume(args["campaign_ids"], _applying_plan_id=_applying_plan_id)
        if action == "set_keyword_bids":
            # Reconstruct BidUpdate dataclasses from the persisted
            # plan args (raw dicts, the apply-plan re-entry path needs
            # the same shape ``BiddingService.apply`` consumed at plan
            # creation time).
            from ..services.bidding import BiddingService, BidUpdate

            bid_svc = BiddingService(
                settings, pipeline=pipeline, store=store, audit_sink=audit_sink
            )
            updates = [
                BidUpdate(
                    keyword_id=u["keyword_id"],
                    new_search_bid_rub=u.get("new_search_bid_rub"),
                    new_network_bid_rub=u.get("new_network_bid_rub"),
                )
                for u in args["updates"]
            ]
            return await bid_svc.apply(updates, _applying_plan_id=_applying_plan_id)
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
    # Use the SAME ``(pipeline, store, audit_sink)`` triple the agent's
    # tools registry constructs, so apply-plan operates on the exact
    # store the agent wrote to and emits into the exact JSONL the agent
    # wrote to. Auditor PR M2.3b MEDIUM: the previous version
    # constructed two PendingPlansStore instances pointing at the same
    # path — fine today, but a future in-memory cache / lock on the
    # store would create a coherence trap. ``build_safety_pair``
    # already encodes the path convention; trust its return value.
    pipeline, store, audit_sink = build_safety_pair(settings)
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


# --------------------------------------------------------------------------
# `rollout` — staged-rollout state inspection and promotion (M2.5).
#
# The ``rollout_stage`` field on Policy controls which actions an agent
# run may attempt:
#   - shadow         → read-only.
#   - assist         → pause + negative keywords + small bid changes.
#   - autonomy_light → bid ±25%, budget ±15%, keyword creation.
#   - autonomy_full  → everything except ``forbidden_operations``.
#
# YAML provides the default; ``rollout promote --to <stage>`` writes a
# state-file that overrides the YAML at boot. Promotions are audit-
# logged via the same ``JsonlSink`` the agent uses for service events,
# so the JSONL is a single chronological record of every operator and
# agent action.
# --------------------------------------------------------------------------


_VALID_STAGES = ("shadow", "assist", "autonomy_light", "autonomy_full")


rollout_app = typer.Typer(
    name="rollout",
    help="Inspect and promote the agent's rollout stage.",
    no_args_is_help=True,
)
app.add_typer(rollout_app, name="rollout")


def _rollout_store(settings: Settings) -> RolloutStateStore:
    """Convention path: next to the audit log. Same path
    ``build_safety_pair`` reads from."""
    return RolloutStateStore(settings.audit_log_path.parent / "rollout_state.json")


@rollout_app.command("status")
def rollout_status_cmd() -> None:
    """Show the current rollout stage and where it came from.

    Reports both the YAML default (Policy.rollout_stage from
    ``agent_policy.yml``) and the state-file override if present, so
    the operator can tell at a glance whether a promote has been
    applied this deployment.
    """
    settings = _bootstrap_settings()
    pipeline, _, _ = build_safety_pair(settings)
    yaml_stage = pipeline.policy.rollout_stage  # already overridden if state-file exists
    state = _rollout_store(settings).load()

    _out.print(f"[bold]effective stage[/bold] {_rich_escape(yaml_stage)}")
    if state is None:
        _out.print("[dim]no rollout_state.json — using Policy default from YAML[/dim]")
    else:
        _out.print(
            f"[dim]state-file: promoted from "
            f"[yellow]{_rich_escape(state.previous_stage)}[/yellow] → "
            f"[green]{_rich_escape(state.stage)}[/green] at "
            f"{state.promoted_at.isoformat(timespec='seconds')} "
            f"by {_rich_escape(state.promoted_by)}[/dim]"
        )


@rollout_app.command("promote")
def rollout_promote_cmd(
    to: Annotated[
        str,
        typer.Option(
            "--to",
            help="Target stage: shadow / assist / autonomy_light / autonomy_full.",
        ),
    ],
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            "-y",
            help="Skip the interactive confirmation (for cron / CI).",
        ),
    ] = False,
    actor: Annotated[
        str | None,
        typer.Option(
            "--actor",
            help="Operator identifier to record in the state-file. "
            "Defaults to the current OS user (getpass.getuser()).",
        ),
    ] = None,
) -> None:
    """Promote (or roll back) the agent's rollout stage.

    Writes ``rollout_state.json`` and emits a
    ``rollout_promote.requested|.ok|.failed`` audit-event triple
    via the configured JsonlSink. Both upgrades and downgrades are
    allowed — downgrade is a deliberate safety win so an operator
    can roll back to ``shadow`` after an incident.

    Exit codes: 0 promoted; 1 invalid stage; 2 operator declined
    confirmation; 3 audit / state-file write failed.
    """
    if to not in _VALID_STAGES:
        _err.print(f"[red]invalid stage:[/red] {to!r}. Valid: {', '.join(_VALID_STAGES)}")
        raise typer.Exit(code=1)

    settings = _bootstrap_settings()
    pipeline, _, audit_sink = build_safety_pair(settings)
    current_stage = pipeline.policy.rollout_stage
    store = _rollout_store(settings)

    operator = actor or _resolve_operator()

    _out.print(
        f"[bold]promote rollout:[/bold] "
        f"[yellow]{_rich_escape(current_stage)}[/yellow] → "
        f"[green]{_rich_escape(to)}[/green]"
    )
    if to == "autonomy_full":
        _out.print(
            "[red]WARNING:[/red] autonomy_full lets the agent perform "
            "every non-forbidden action. Verify success-gate metrics "
            "before promoting."
        )

    if not yes:
        try:
            confirmed = typer.confirm("proceed?", default=False)
        except (EOFError, KeyboardInterrupt):
            _err.print("[red]aborted[/red]")
            raise typer.Exit(code=2) from None
        if not confirmed:
            _err.print("[red]aborted[/red]")
            raise typer.Exit(code=2)

    new_state = RolloutState(
        # ``to`` was validated against ``_VALID_STAGES`` above, so
        # pydantic Literal acceptance is guaranteed at runtime.
        stage=to,
        promoted_at=datetime.now(UTC),
        promoted_by=operator,
        previous_stage=current_stage,
    )

    try:
        asyncio.run(_persist_promotion(audit_sink, store, new_state, operator))
    except Exception as exc:
        _err.print(f"[red]promote failed:[/red] {type(exc).__name__}: {_rich_escape(str(exc))}")
        raise typer.Exit(code=3) from exc

    _out.print(f"[green]promoted[/green] to {_rich_escape(to)}")


async def _persist_promotion(
    audit_sink: AuditSink,
    store: RolloutStateStore,
    new_state: RolloutState,
    operator: str,
) -> None:
    """Save state-file under an audit envelope so promote events land
    in the JSONL alongside agent activity. Audit emit failures do NOT
    propagate (we want the state-file write to succeed regardless),
    but state-file write failures DO propagate so the CLI surfaces
    them as exit 3."""
    args = {
        "from_stage": new_state.previous_stage,
        "to_stage": new_state.stage,
        "actor": operator,
    }
    async with audit_action(
        audit_sink,
        actor="human",
        action="rollout_promote",
        resource="rollout",
        args=args,
    ) as ctx:
        store.save(new_state)
        ctx.set_result(
            {
                "status": "promoted",
                "from_stage": new_state.previous_stage,
                "to_stage": new_state.stage,
            }
        )


def _resolve_operator() -> str:
    """OS-username fallback for ``--actor`` when not supplied."""
    import getpass

    try:
        return getpass.getuser()
    except Exception:
        return "unknown"


# Re-export for tests / type-checkers that look at the public surface.
_ = AuditEvent


# --------------------------------------------------------------------------
# `mcp` — Model Context Protocol server (M3).
#
# ``yadirect-agent mcp serve`` exposes the seven existing tools over MCP
# stdio so a Claude Desktop / Claude Code agent can drive the account
# through the same handlers the in-process agent loop uses. By default
# write tools (pause / resume / set_*) are NOT registered — the LLM
# literally cannot see them. Use ``--allow-write`` (or
# ``MCP_ALLOW_WRITE=true``) to opt in. Mutations still flow through the
# safety pipeline + plan→confirm→execute, so an MCP-driven mutation
# returns ``{status: pending, plan_id: ...}`` and the operator must run
# ``yadirect-agent apply-plan <id>`` from a terminal to actually apply.
# --------------------------------------------------------------------------


mcp_app = typer.Typer(
    name="mcp",
    help="Run yadirect-agent as a Model Context Protocol server.",
    no_args_is_help=True,
)
app.add_typer(mcp_app, name="mcp")


@mcp_app.command("serve")
def mcp_serve_cmd(
    allow_write: Annotated[
        bool,
        typer.Option(
            "--allow-write",
            help=(
                "Expose mutating tools (pause / resume / set_*) to the MCP "
                "client. Off by default — mutations still flow through "
                "@requires_plan and need an out-of-band apply-plan from "
                "the operator's terminal. Equivalent env var: "
                "MCP_ALLOW_WRITE=true."
            ),
        ),
    ] = False,
) -> None:
    """Run the MCP server over stdio. Foreground process.

    Designed to be launched by Claude Desktop / Claude Code via the
    ``mcpServers`` configuration block (see docs/CLAUDE_DESKTOP.md).
    Logs go to stderr in JSON; stdout is reserved for the MCP
    protocol stream.
    """
    import os

    from ..mcp.server import build_mcp_server

    settings = _bootstrap_settings()
    # ``--allow-write`` CLI flag wins; otherwise fall back to env.
    if not allow_write and os.environ.get("MCP_ALLOW_WRITE", "").lower() in {
        "1",
        "true",
        "yes",
    }:
        allow_write = True

    handle = build_mcp_server(settings, allow_write=allow_write)
    asyncio.run(_run_mcp_stdio(handle))


async def _run_mcp_stdio(handle: Any) -> None:
    """Connect the MCP server to stdio and pump messages until the
    client disconnects. Factored out so unit tests can construct
    the handle without binding to stdio.
    """
    from mcp.server.stdio import stdio_server

    async with stdio_server() as (read_stream, write_stream):
        await handle.server.run(
            read_stream,
            write_stream,
            handle.server.create_initialization_options(),
        )


if __name__ == "__main__":  # pragma: no cover
    app()
