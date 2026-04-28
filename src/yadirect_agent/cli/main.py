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
from pathlib import Path
from typing import Annotated, Any

import structlog
import typer
from rich.console import Console
from rich.markup import escape as _rich_escape
from rich.table import Table

from .. import __version__
from ..agent.cost import CostStore
from ..agent.executor import (
    InvalidPlanStateError,
    PlanRejected,
    apply_plan,
)
from ..agent.loop import Agent, AgentLoopError, AgentRun
from ..agent.plans import OperationPlan, PendingPlansStore
from ..agent.rationale_store import RationaleStore
from ..agent.tools import build_default_registry, build_safety_pair
from ..audit import AuditEvent, AuditSink, audit_action
from ..auth.callback_server import OAuthCallbackError
from ..auth.keychain import KeyringTokenStore
from ..auth.login_flow import perform_login
from ..config import Settings, get_settings
from ..exceptions import AuthError
from ..logging import configure_logging
from ..models.health import default_window
from ..rollout import RolloutState, RolloutStateStore
from ..services.campaigns import CampaignService, CampaignSummary
from ..services.health_check import HealthCheckService
from .auth import (
    LOGIN_BROWSER_FALLBACK_HINT,
    LOGIN_EXCHANGE_ERROR_PREFIX,
    LOGIN_KEYCHAIN_NOTE,
    LOGIN_OAUTH_ERROR_PREFIX,
    LOGIN_OPENING_BROWSER_HINT,
    LOGIN_SUCCESS,
    LOGIN_TIMEOUT_HINT,
    LOGOUT_SUCCESS,
    STATUS_NOT_LOGGED_IN,
)
from .auth import render_status_text as render_auth_status_text
from .auth import status_dict as auth_status_dict
from .cost import aggregate_for_status, render_status_json, render_status_text
from .doctor import (
    CheckResult,
    check_anthropic,
    check_direct_sandbox,
    check_env,
    check_policy_file,
)
from .health import render_report_json, render_report_text
from .install import (
    ConfigError as ClaudeConfigError,
)
from .install import (
    install_into_config,
    resolve_config_path,
    uninstall_from_config,
)
from .rationale import render_list_text, render_show_json, render_show_text

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
            help="ON | OFF | SUSPENDED | ENDED | CONVERTED | ARCHIVED.",
        ),
    ] = None,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Emit JSON to stdout instead of a table."),
    ] = False,
) -> None:
    """Direct pass-through to CampaignService — no LLM involved.

    ``--state`` filters the result client-side. We always fetch
    ``list_all()`` and apply the filter in Python rather than
    routing through ``list_active()`` (which hardcodes ON+SUSPENDED
    and was previously called for every non-empty ``state``,
    silently ignoring the requested value — auditor finding).
    Invalid state values are rejected with a non-zero exit so a
    typo can't masquerade as "no campaigns in that state".
    """
    from ..models.campaigns import CampaignState

    requested_state: str | None = None
    if state is not None:
        normalised = state.strip().upper()
        valid = {s.value for s in CampaignState}
        if normalised not in valid:
            valid_list = ", ".join(sorted(valid))
            raise typer.BadParameter(
                f"invalid state {state!r}; expected one of: {valid_list}",
                param_hint="--state",
            )
        requested_state = normalised

    settings = _bootstrap_settings()
    service = CampaignService(settings)

    async def fetch() -> list[CampaignSummary]:
        summaries = await service.list_all()
        if requested_state is None:
            return summaries
        return [s for s in summaries if s.state == requested_state]

    summaries = asyncio.run(fetch())
    if as_json:
        typer.echo(json.dumps([asdict(s) for s in summaries], ensure_ascii=False))
        return
    _render_campaigns_table(summaries)


# --------------------------------------------------------------------------
# `health` — rule-based account health check (M15.5.1).
# --------------------------------------------------------------------------


@app.command("health")
def health_cmd(
    days: Annotated[
        int,
        typer.Option(
            "--days",
            min=1,
            max=90,
            help="Window length, ending yesterday. Default 7.",
        ),
    ] = 7,
    goal_id: Annotated[
        int | None,
        typer.Option(
            "--goal-id",
            min=1,
            help=(
                "Metrika goal id to count conversions against. "
                "Without it, conversion-based rules silently skip. "
                "Must be a positive integer."
            ),
        ),
    ] = None,
    as_json: Annotated[
        bool,
        typer.Option(
            "--json",
            help="Emit JSON to stdout instead of a table.",
        ),
    ] = False,
) -> None:
    """Run rule-based account health check, emit findings.

    No LLM involved — entirely deterministic on Direct + Metrika data.
    Use this for the first read of a new account and as a daily probe
    in cron. Exit code is 0 if no HIGH-severity findings, 1 if any
    HIGH finding fired (suitable for cron alerting).
    """
    settings = _bootstrap_settings()

    async def fetch() -> Any:
        async with HealthCheckService(settings) as svc:
            return await svc.run_account_check(
                date_range=default_window(days=days),
                goal_id=goal_id,
            )

    report = asyncio.run(fetch())

    if as_json:
        typer.echo(render_report_json(report))
    else:
        render_report_text(_out, report)

    from ..models.health import Severity as _Sev

    if report.findings_by_severity(_Sev.HIGH):
        raise typer.Exit(code=1)


# --------------------------------------------------------------------------
# `cost` subapp — LLM spend tracking (M21).
# --------------------------------------------------------------------------


cost_app = typer.Typer(
    name="cost",
    help="Inspect LLM spend across agent runs.",
    no_args_is_help=True,
)
app.add_typer(cost_app, name="cost")


def _cost_store(settings: Settings) -> CostStore:
    """Standard location: sibling to the audit log."""
    path = settings.audit_log_path.parent / "cost.jsonl"
    return CostStore(path)


@cost_app.command("status")
def cost_status_cmd(
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Emit JSON instead of formatted output."),
    ] = False,
) -> None:
    """Show current and previous month LLM spend, with end-of-month projection.

    Honours ``AGENT_MONTHLY_LLM_BUDGET_RUB`` to color-code current vs budget.
    No enforcement happens here — this is observability only. Hard
    auto-degrade to ``--no-llm`` mode is M21.2 follow-up.
    """
    settings = _bootstrap_settings()
    store = _cost_store(settings)
    records = store.all_records()
    summaries = aggregate_for_status(records)

    if as_json:
        typer.echo(render_status_json(summaries, settings))
    else:
        render_status_text(_out, summaries, settings)


# --------------------------------------------------------------------------
# `auth` subapp — Yandex OAuth login / status / revoke (M15.3).
# --------------------------------------------------------------------------


auth_app = typer.Typer(
    name="auth",
    help="Yandex OAuth: log in (PKCE flow), inspect status, revoke.",
    no_args_is_help=True,
)
app.add_typer(auth_app, name="auth")


@auth_app.command("login")
def auth_login_cmd() -> None:
    """Run the Yandex OAuth PKCE flow and persist the token to the OS keychain.

    Opens the operator's default browser to the Yandex consent
    page, runs a one-shot HTTP server on ``localhost:8765`` to
    catch the redirect, exchanges the code, and writes the
    TokenSet under ``yadirect-agent / oauth`` in the keychain.

    Exit codes:
    - 0  — success.
    - 2  — user denied, callback timed out, or token exchange
      rejected. The CLI surfaces the cause; the operator re-runs.
    """
    _out.print(LOGIN_OPENING_BROWSER_HINT)
    _out.print(LOGIN_BROWSER_FALLBACK_HINT)
    try:
        token = asyncio.run(perform_login())
    except OAuthCallbackError as exc:
        _err.print(f"[red]{LOGIN_OAUTH_ERROR_PREFIX}:[/red] {_rich_escape(str(exc))}")
        raise typer.Exit(code=2) from exc
    except TimeoutError as exc:
        _err.print(f"[red]{LOGIN_TIMEOUT_HINT}[/red]")
        raise typer.Exit(code=2) from exc
    except AuthError as exc:
        _err.print(f"[red]{LOGIN_EXCHANGE_ERROR_PREFIX}:[/red] {_rich_escape(str(exc))}")
        raise typer.Exit(code=2) from exc

    _out.print(f"[green]{LOGIN_SUCCESS}[/green]")
    _out.print(f"  scope: {', '.join(token.scope)}")
    _out.print(f"  expires_at: {token.expires_at.isoformat()}")
    _out.print(LOGIN_KEYCHAIN_NOTE)


@auth_app.command("status")
def auth_status_cmd(
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Emit JSON instead of formatted output."),
    ] = False,
) -> None:
    """Show whether a TokenSet is stored, plus its scope + expiry (masked).

    Exit codes:
    - 0  — token present.
    - 1  — not logged in. Cron-friendly so a wrapper can alert.
    """
    token = KeyringTokenStore().load()
    if token is None:
        if as_json:
            typer.echo(json.dumps({"status": "not_logged_in"}))
        else:
            _err.print(STATUS_NOT_LOGGED_IN)
        raise typer.Exit(code=1)

    if as_json:
        typer.echo(json.dumps(auth_status_dict(token)))
        return
    _out.print(render_auth_status_text(token))


@auth_app.command("logout")
def auth_logout_cmd() -> None:
    """Clear the stored TokenSet from the OS keychain.

    Local-only operation: deletes the keychain slot. Yandex OAuth
    has no public revocation endpoint, so the refresh token issued
    to us remains valid server-side until manually revoked at
    ``yandex.ru/profile/access``. The CLI message says so.

    Idempotent: running on a fresh install (no record) is a no-op
    exit-zero, so a setup script can call ``auth logout`` then
    ``auth login`` without conditional logic.
    """
    KeyringTokenStore().delete()
    _out.print(f"[green]{LOGOUT_SUCCESS}[/green]")


# --------------------------------------------------------------------------
# `install-into-claude-desktop` / `uninstall-from-claude-desktop` (M15.2).
# --------------------------------------------------------------------------


def _resolve_install_path(config_path: Path | None) -> Path:
    """Decide whether to use the explicit ``--config-path`` or the OS default."""
    if config_path is not None:
        return config_path
    try:
        return resolve_config_path()
    except ClaudeConfigError as exc:
        # Escape the exception message — it embeds the env-var value
        # (XDG_CONFIG_HOME, APPDATA) which is operator-controlled and
        # could carry Rich markup (auditor M15.2 LOW-4 / MEDIUM-2).
        _err.print(f"[red]error:[/red] {_rich_escape(str(exc))}")
        _err.print("Pass --config-path to override.")
        raise typer.Exit(code=1) from exc


@app.command("install-into-claude-desktop")
def install_into_claude_desktop_cmd(
    config_path: Annotated[
        Path | None,
        typer.Option(
            "--config-path",
            help=(
                "Explicit path to claude_desktop_config.json. "
                "Default: OS-conventional location (macOS / Windows / Linux)."
            ),
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Show what would change without writing.",
        ),
    ] = False,
) -> None:
    """Wire yadirect-agent into the Claude Desktop config.

    Idempotent — re-running on an already-installed config is a no-op.
    Existing config is backed up with a timestamped suffix before any
    write. Operator-set MCP servers and unrelated top-level fields
    are preserved verbatim.

    Run this once after ``pip install yadirect-agent``, then restart
    Claude Desktop to see the new tool.
    """
    path = _resolve_install_path(config_path)
    try:
        result = install_into_config(path, dry_run=dry_run)
    except ClaudeConfigError as exc:
        _err.print(f"[red]error:[/red] {_rich_escape(str(exc))}")
        raise typer.Exit(code=1) from exc

    # All path-shaped values come from operator input (--config-path
    # or XDG_CONFIG_HOME / APPDATA env vars) and must pass through
    # _rich_escape — same hardening as M15.5.1 HIGH-1. (auditor
    # M15.2 MEDIUM-2.)
    cfg_str = _rich_escape(str(result.config_path))
    prefix = "[dim](dry-run)[/dim] " if result.dry_run else ""
    if result.action == "added":
        _out.print(f"{prefix}[green]✓[/green] Added yadirect-agent to {cfg_str}")
    elif result.action == "updated":
        _out.print(
            f"{prefix}[yellow]✓[/yellow] Updated stale yadirect-agent entry at {cfg_str}",
        )
    else:  # already_installed
        _out.print(f"{prefix}[dim]Already installed at {cfg_str}[/dim]")

    if result.backup_path is not None:
        backup_str = _rich_escape(str(result.backup_path))
        _out.print(f"{prefix}[dim]Backed up previous config to {backup_str}[/dim]")

    if not result.dry_run and result.action != "already_installed":
        _out.print(
            "\n[bold]Next:[/bold] Restart Claude Desktop. "
            "The new tool appears under the slider icon in the chat input.",
        )


@app.command("uninstall-from-claude-desktop")
def uninstall_from_claude_desktop_cmd(
    config_path: Annotated[
        Path | None,
        typer.Option(
            "--config-path",
            help="Explicit path to claude_desktop_config.json.",
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Show what would change without writing."),
    ] = False,
) -> None:
    """Remove the yadirect-agent entry from Claude Desktop config.

    Other MCP servers and unrelated top-level fields are preserved.
    No-op when the entry is absent or the config file does not exist.
    """
    path = _resolve_install_path(config_path)
    try:
        result = uninstall_from_config(path, dry_run=dry_run)
    except ClaudeConfigError as exc:
        _err.print(f"[red]error:[/red] {_rich_escape(str(exc))}")
        raise typer.Exit(code=1) from exc

    cfg_str = _rich_escape(str(result.config_path))
    prefix = "[dim](dry-run)[/dim] " if result.dry_run else ""
    if result.action == "removed":
        _out.print(f"{prefix}[green]✓[/green] Removed yadirect-agent from {cfg_str}")
        if result.backup_path is not None:
            backup_str = _rich_escape(str(result.backup_path))
            _out.print(f"{prefix}[dim]Backed up previous config to {backup_str}[/dim]")
        if not result.dry_run:
            _out.print("\n[dim]Restart Claude Desktop to drop the tool from the menu.[/dim]")
    else:  # not_installed
        _out.print(
            f"{prefix}[dim]yadirect-agent not installed at {cfg_str} — nothing to do.[/dim]",
        )


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
# `rationale` subapp — read-back of recorded decision rationales (M20.3 slice).
# --------------------------------------------------------------------------


rationale_app = typer.Typer(
    name="rationale",
    help="Inspect recorded rationales for past agent decisions.",
    no_args_is_help=True,
)
app.add_typer(rationale_app, name="rationale")


def _rationale_store(settings: Settings) -> RationaleStore:
    """Standard location: next to the audit log."""
    path = settings.audit_log_path.parent / "rationale.jsonl"
    return RationaleStore(path)


@rationale_app.command("show")
def rationale_show_cmd(
    decision_id: Annotated[
        str,
        typer.Argument(help="decision_id (== plan_id of the original plan)."),
    ],
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Emit JSON to stdout instead of formatted text."),
    ] = False,
) -> None:
    """Show the full rationale for one decision."""
    settings = _bootstrap_settings()
    store = _rationale_store(settings)
    rationale = store.get(decision_id)
    if rationale is None:
        _err.print(f"[red]no rationale with decision_id {decision_id!r}[/red]")
        raise typer.Exit(code=1)

    if as_json:
        typer.echo(render_show_json(rationale))
    else:
        render_show_text(_out, rationale)


@rationale_app.command("list")
def rationale_list_cmd(
    days: Annotated[
        int,
        typer.Option(
            "--days",
            min=1,
            max=365,
            help="Window length, ending now. Default 7.",
        ),
    ] = 7,
    campaign: Annotated[
        int | None,
        typer.Option(
            "--campaign",
            min=1,
            help="Filter to rationales whose resource_ids include this campaign.",
        ),
    ] = None,
) -> None:
    """List recent rationales, newest first."""
    settings = _bootstrap_settings()
    store = _rationale_store(settings)

    if campaign is not None:
        # Defence in depth: typer ``min=1`` already rejects ``--days 0``,
        # but the campaign branch goes via ``list_for_resource`` (no
        # built-in window guard) rather than ``list_recent`` (which has
        # one). Refusing here mirrors the behaviour the non-campaign
        # branch enforces through the store. (auditor M20 MEDIUM-1.)
        if days <= 0:
            _err.print("[red]--days must be >= 1[/red]")
            raise typer.Exit(code=1)
        rationales = store.list_for_resource(campaign_id=campaign)
        # Apply the day window client-side after the campaign filter.
        from datetime import UTC, datetime, timedelta

        cutoff = datetime.now(UTC) - timedelta(days=days)
        rationales = [r for r in rationales if r.timestamp >= cutoff]
    else:
        rationales = store.list_recent(days=days)

    render_list_text(_out, rationales)


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
            # Reconstruct ``BidUpdate`` instances from the persisted
            # plan args. ``BidUpdate.model_validate`` (rather than a
            # constructor splat) routes through pydantic so:
            # - ``extra="forbid"`` raises on schema drift (a future
            #   field added to BidUpdate but missing from a stored
            #   plan surfaces as a clean ValidationError);
            # - field-level constraints (``ge=0`` on bids) re-fire
            #   on replay even if the writer somehow corrupted them.
            # Auditor M2-bidding M-1.
            from ..services.bidding import BiddingService, BidUpdate

            bid_svc = BiddingService(
                settings, pipeline=pipeline, store=store, audit_sink=audit_sink
            )
            updates = [BidUpdate.model_validate(u) for u in args["updates"]]
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
    ``mcpServers`` configuration block (see docs/OPERATING.md).
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
