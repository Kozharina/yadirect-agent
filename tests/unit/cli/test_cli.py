"""Typer CLI smoke tests.

We use `typer.testing.CliRunner` to drive commands in-process. The agent
itself is exercised through `tests/unit/agent/*`; here we check only the
CLI plumbing:

- `--version` prints and exits zero.
- `list-campaigns --json` returns a valid JSON document (service mocked).
- `run "..."` dispatches into `Agent.run` (monkeypatched) and renders output.

We monkeypatch `yadirect_agent.cli.main.get_settings` to bypass the real
env resolution — `Settings()` would otherwise fail without `.env` secrets.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from typer.testing import CliRunner

from yadirect_agent import __version__
from yadirect_agent.cli.main import app
from yadirect_agent.services.campaigns import CampaignSummary


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def _patch_bootstrap(monkeypatch: pytest.MonkeyPatch, settings: Any) -> None:
    """Wire get_settings and configure_logging to be no-ops bound to our fixture."""
    monkeypatch.setattr("yadirect_agent.cli.main.get_settings", lambda: settings)
    monkeypatch.setattr("yadirect_agent.cli.main.configure_logging", lambda _s: None)


def test_version_flag_prints_and_exits(runner: CliRunner) -> None:
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_list_campaigns_json_output(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    _patch_bootstrap: None,
) -> None:
    from yadirect_agent.services.campaigns import CampaignService

    async def fake_list_all(self: CampaignService, _limit: int = 500) -> list:
        return [
            CampaignSummary(
                id=1,
                name="alpha",
                state="ON",
                status="ACCEPTED",
                type="TEXT_CAMPAIGN",
                daily_budget_rub=500.0,
            )
        ]

    monkeypatch.setattr(CampaignService, "list_all", fake_list_all)

    result = runner.invoke(app, ["list-campaigns", "--json"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout.strip().splitlines()[-1])
    assert data == [
        {
            "id": 1,
            "name": "alpha",
            "state": "ON",
            "status": "ACCEPTED",
            "type": "TEXT_CAMPAIGN",
            "daily_budget_rub": 500.0,
        }
    ]


def test_list_campaigns_empty_state(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    _patch_bootstrap: None,
) -> None:
    from yadirect_agent.services.campaigns import CampaignService

    async def fake_list_all(self: CampaignService, _limit: int = 500) -> list:
        return []

    monkeypatch.setattr(CampaignService, "list_all", fake_list_all)

    result = runner.invoke(app, ["list-campaigns"])

    assert result.exit_code == 0
    assert "no campaigns" in result.output


def test_run_dispatches_into_agent_run(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    _patch_bootstrap: None,
) -> None:
    # Intercept _run_agent so we don't try to construct a real AsyncAnthropic.
    from yadirect_agent.agent.loop import AgentRun

    captured: dict[str, str] = {}

    async def fake_run(settings: Any, task: str) -> AgentRun:
        captured["task"] = task
        return AgentRun(
            trace_id="tr",
            final_text="I did the thing.",
            tool_calls=[],
            iterations=1,
            input_tokens=10,
            output_tokens=20,
            stop_reason="end_turn",
        )

    monkeypatch.setattr("yadirect_agent.cli.main._run_agent", fake_run)

    result = runner.invoke(app, ["run", "do the thing"])

    assert result.exit_code == 0, result.output
    assert captured["task"] == "do the thing"
    assert "I did the thing." in result.output
    assert "trace_id=tr" in result.output


# --------------------------------------------------------------------------
# `plans` subcommand (M2.2 data layer).
# --------------------------------------------------------------------------


def _write_one_plan(store_path: Any) -> None:
    """Seed pending_plans.jsonl with a single minimal plan."""
    from datetime import UTC, datetime

    from yadirect_agent.agent.plans import OperationPlan, PendingPlansStore

    plan = OperationPlan(
        plan_id="plan-a",
        created_at=datetime(2026, 4, 24, 10, 0, tzinfo=UTC),
        action="set_campaign_budget",
        resource_type="campaign",
        resource_ids=[42],
        args={"campaign_id": 42, "new_budget_rub": 800},
        preview="raise campaign 42 budget 500→800 RUB",
        reason="change exceeds auto-approval ceiling of +20%",
    )
    PendingPlansStore(store_path).append(plan)


def test_plans_list_shows_pending_plan(
    runner: CliRunner,
    _patch_bootstrap: None,
    settings: Any,
) -> None:
    # Seed one plan next to the audit log (where the CLI looks by default).
    plans_path = settings.audit_log_path.parent / "pending_plans.jsonl"
    _write_one_plan(plans_path)

    result = runner.invoke(app, ["plans", "list"])

    assert result.exit_code == 0, result.output
    assert "plan-a" in result.output
    # Rich table truncates long strings with ellipsis in narrow
    # terminals, so just check the fields we know won't wrap.
    assert "pending" in result.output
    assert "set_campaign" in result.output  # prefix is always visible


def test_plans_list_says_no_plans_when_store_is_empty(
    runner: CliRunner,
    _patch_bootstrap: None,
    settings: Any,
) -> None:
    result = runner.invoke(app, ["plans", "list"])

    assert result.exit_code == 0
    assert "no plans" in result.output.lower()


def test_plans_show_prints_full_detail(
    runner: CliRunner,
    _patch_bootstrap: None,
    settings: Any,
) -> None:
    plans_path = settings.audit_log_path.parent / "pending_plans.jsonl"
    _write_one_plan(plans_path)

    result = runner.invoke(app, ["plans", "show", "plan-a"])

    assert result.exit_code == 0, result.output
    assert "plan-a" in result.output
    assert "reason" in result.output.lower()
    assert "raise campaign 42" in result.output


def test_plans_show_returns_nonzero_for_unknown_id(
    runner: CliRunner,
    _patch_bootstrap: None,
    settings: Any,
) -> None:
    result = runner.invoke(app, ["plans", "show", "does-not-exist"])

    assert result.exit_code == 1
    assert "no plan" in result.output.lower()


# --------------------------------------------------------------------------
# `apply-plan` command (M2.2 part 3b2).
# --------------------------------------------------------------------------


def _write_full_plan_with_review_context(store_path: Any, plan_id: str = "plan-x") -> None:
    """Seed a realistic pending plan with a serialised ReviewContext.

    The CLI's apply-plan path requires review_context to be non-null
    so it can re-review against the original snapshot — a plan written
    by hand with the basic ``OperationPlan(...)`` constructor only
    (which has review_context=None) would hit InvalidPlanStateError.
    """
    from datetime import UTC, datetime

    from yadirect_agent.agent.pipeline import ReviewContext, serialize_review_context
    from yadirect_agent.agent.plans import OperationPlan, PendingPlansStore
    from yadirect_agent.agent.safety import (
        AccountBudgetSnapshot,
        BudgetChange,
        CampaignBudget,
    )

    ctx = ReviewContext(
        budget_snapshot=AccountBudgetSnapshot(
            campaigns=[
                CampaignBudget(id=42, name="alpha", daily_budget_rub=500.0, state="ON"),
            ]
        ),
        budget_changes=[BudgetChange(campaign_id=42, new_daily_budget_rub=800)],
    )
    plan = OperationPlan(
        plan_id=plan_id,
        created_at=datetime(2026, 4, 24, 10, 0, tzinfo=UTC),
        action="set_campaign_budget",
        resource_type="campaign",
        resource_ids=[42],
        args={"campaign_id": 42, "budget_rub": 800},
        preview="raise campaign 42 budget 500→800 RUB",
        reason="change exceeds auto-approval ceiling of +20%",
        review_context=serialize_review_context(ctx),
    )
    PendingPlansStore(store_path).append(plan)


def test_apply_plan_unknown_id_exits_nonzero(
    runner: CliRunner,
    _patch_bootstrap: None,
    settings: Any,
) -> None:
    result = runner.invoke(app, ["apply-plan", "does-not-exist"])
    assert result.exit_code == 1
    assert "not found" in result.output.lower()


def test_apply_plan_already_applied_exits_nonzero(
    runner: CliRunner,
    _patch_bootstrap: None,
    settings: Any,
) -> None:
    from yadirect_agent.agent.plans import PendingPlansStore

    plans_path = settings.audit_log_path.parent / "pending_plans.jsonl"
    _write_full_plan_with_review_context(plans_path, plan_id="plan-applied")
    PendingPlansStore(plans_path).update_status("plan-applied", "applied")

    result = runner.invoke(app, ["apply-plan", "plan-applied"])
    assert result.exit_code == 1
    assert "applied" in result.output.lower() or "not pending" in result.output.lower()


def test_apply_plan_re_review_reject_exits_with_code_2(
    runner: CliRunner,
    _patch_bootstrap: None,
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-review at apply-plan time may return reject (snapshot drift,
    rollout-stage tightened, etc.). Status moves to ``rejected`` and
    the CLI exits with code 2 so cron / scripts can branch on it.
    """
    from yadirect_agent.agent.pipeline import SafetyDecision, SafetyPipeline
    from yadirect_agent.agent.safety import CheckResult

    plans_path = settings.audit_log_path.parent / "pending_plans.jsonl"
    _write_full_plan_with_review_context(plans_path, plan_id="plan-reject")

    def stub_review(self: SafetyPipeline, plan: Any, ctx: Any) -> SafetyDecision:
        return SafetyDecision(
            status="reject",
            reason="account cap lowered since plan creation",
            blocking_checks=[CheckResult(status="blocked", reason="budget_cap: x")],
        )

    monkeypatch.setattr(SafetyPipeline, "review", stub_review)

    result = runner.invoke(app, ["apply-plan", "plan-reject"])
    assert result.exit_code == 2, result.output
    assert "reject" in result.output.lower()


def test_apply_plan_happy_path_marks_applied(
    runner: CliRunner,
    _patch_bootstrap: None,
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: re-review allows, executor runs, plan moves to applied."""
    from yadirect_agent.agent.pipeline import SafetyDecision, SafetyPipeline
    from yadirect_agent.agent.plans import PendingPlansStore
    from yadirect_agent.services.campaigns import CampaignService

    plans_path = settings.audit_log_path.parent / "pending_plans.jsonl"
    _write_full_plan_with_review_context(plans_path, plan_id="plan-go")

    def stub_review(self: SafetyPipeline, plan: Any, ctx: Any) -> SafetyDecision:
        return SafetyDecision(status="allow", reason="ok")

    captured: list[tuple[int, int, str | None]] = []

    async def fake_set_budget(
        self: CampaignService,
        campaign_id: int,
        budget_rub: int,
        *,
        _applying_plan_id: str | None = None,
    ) -> None:
        # Auditor LOW-3: the bypass kwarg MUST reach the wrapped service
        # method or the @requires_plan decorator will re-enter the
        # full pipeline review on a plan that's already pending. Pin
        # the contract by capturing the kwarg.
        captured.append((campaign_id, budget_rub, _applying_plan_id))

    monkeypatch.setattr(SafetyPipeline, "review", stub_review)
    monkeypatch.setattr(CampaignService, "set_daily_budget", fake_set_budget)

    result = runner.invoke(app, ["apply-plan", "plan-go"])

    assert result.exit_code == 0, result.output
    assert "applied" in result.output.lower()
    # Bypass kwarg forwarded with the right plan_id.
    assert captured == [(42, 800, "plan-go")]
    final = PendingPlansStore(plans_path).get("plan-go")
    assert final is not None
    assert final.status == "applied"


def test_apply_plan_failed_status_cannot_be_re_applied(
    runner: CliRunner,
    _patch_bootstrap: None,
    settings: Any,
) -> None:
    """Auditor LOW-1: a plan in ``failed`` (executor raised on a prior
    apply) must not be silently retryable through apply-plan. Operators
    triage the failure reason and propose a fresh plan.
    """
    from yadirect_agent.agent.plans import PendingPlansStore

    plans_path = settings.audit_log_path.parent / "pending_plans.jsonl"
    _write_full_plan_with_review_context(plans_path, plan_id="plan-failed")
    PendingPlansStore(plans_path).update_status("plan-failed", "failed")

    result = runner.invoke(app, ["apply-plan", "plan-failed"])

    assert result.exit_code == 1
    assert "failed" in result.output.lower()


def test_apply_plan_unknown_action_exits_3(
    runner: CliRunner,
    _patch_bootstrap: None,
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auditor LOW-2: a plan whose ``action`` string isn't in the CLI
    router (e.g. a future ``set_keyword_bids`` action that hasn't been
    wired yet) must surface as a clean failure with exit 3, not silently
    succeed. The plan moves to ``failed`` status so subsequent
    apply-plan calls see the terminal state.
    """
    from datetime import UTC, datetime

    from yadirect_agent.agent.pipeline import (
        ReviewContext,
        SafetyDecision,
        SafetyPipeline,
        serialize_review_context,
    )
    from yadirect_agent.agent.plans import OperationPlan, PendingPlansStore

    plans_path = settings.audit_log_path.parent / "pending_plans.jsonl"
    plan = OperationPlan(
        plan_id="plan-mystery",
        created_at=datetime(2026, 4, 24, 10, 0, tzinfo=UTC),
        action="set_keyword_bids",  # not in the router yet
        resource_type="keyword",
        resource_ids=[1],
        args={"updates": []},
        preview="bid update",
        reason="needs confirm",
        review_context=serialize_review_context(ReviewContext()),
    )
    PendingPlansStore(plans_path).append(plan)

    def stub_review(self: SafetyPipeline, plan_arg: Any, ctx: Any) -> SafetyDecision:
        return SafetyDecision(status="allow", reason="ok")

    monkeypatch.setattr(SafetyPipeline, "review", stub_review)

    result = runner.invoke(app, ["apply-plan", "plan-mystery"])

    assert result.exit_code == 3, result.output
    assert "unknown action" in result.output.lower()
    final = PendingPlansStore(plans_path).get("plan-mystery")
    assert final is not None
    assert final.status == "failed"


# --------------------------------------------------------------------------
# `rollout` subcommand (M2.5).
# --------------------------------------------------------------------------


def test_rollout_status_reports_yaml_default_when_no_state_file(
    runner: CliRunner,
    _patch_bootstrap: None,
    settings: Any,
) -> None:
    """Fresh deployment (no rollout_state.json) → YAML default surfaces;
    output mentions absence of the state-file."""
    # Write a YAML so build_safety_pair has something to load.
    settings.agent_policy_path.parent.mkdir(parents=True, exist_ok=True)
    settings.agent_policy_path.write_text(
        "account_daily_budget_cap_rub: 50000\nrollout_stage: shadow\n"
    )

    result = runner.invoke(app, ["rollout", "status"])

    assert result.exit_code == 0, result.output
    assert "shadow" in result.output
    assert "no rollout_state.json" in result.output.lower()


def test_rollout_status_reports_state_file_override(
    runner: CliRunner,
    _patch_bootstrap: None,
    settings: Any,
) -> None:
    """When a state-file is present, ``status`` reports the override
    path (previous → current with timestamp + actor)."""
    from datetime import UTC, datetime

    from yadirect_agent.rollout import RolloutState, RolloutStateStore

    settings.agent_policy_path.parent.mkdir(parents=True, exist_ok=True)
    settings.agent_policy_path.write_text(
        "account_daily_budget_cap_rub: 50000\nrollout_stage: shadow\n"
    )
    state_path = settings.audit_log_path.parent / "rollout_state.json"
    RolloutStateStore(state_path).save(
        RolloutState(
            stage="assist",
            promoted_at=datetime(2026, 4, 27, 12, 0, tzinfo=UTC),
            promoted_by="ops@example.com",
            previous_stage="shadow",
        )
    )

    result = runner.invoke(app, ["rollout", "status"])

    assert result.exit_code == 0, result.output
    assert "assist" in result.output
    assert "ops@example.com" in result.output
    # Both stages mentioned (previous → current).
    assert "shadow" in result.output


def test_rollout_promote_invalid_stage_exits_1(
    runner: CliRunner,
    _patch_bootstrap: None,
    settings: Any,
) -> None:
    settings.agent_policy_path.parent.mkdir(parents=True, exist_ok=True)
    settings.agent_policy_path.write_text(
        "account_daily_budget_cap_rub: 50000\nrollout_stage: shadow\n"
    )

    result = runner.invoke(app, ["rollout", "promote", "--to", "halfway"])

    assert result.exit_code == 1, result.output
    assert "invalid stage" in result.output.lower()


def test_rollout_promote_writes_state_file_and_emits_audit(
    runner: CliRunner,
    _patch_bootstrap: None,
    settings: Any,
) -> None:
    """Happy path: --yes skips confirmation; state-file written; audit
    JSONL contains rollout_promote.requested + rollout_promote.ok."""
    from yadirect_agent.rollout import RolloutStateStore

    settings.agent_policy_path.parent.mkdir(parents=True, exist_ok=True)
    settings.agent_policy_path.write_text(
        "account_daily_budget_cap_rub: 50000\nrollout_stage: shadow\n"
    )

    result = runner.invoke(
        app,
        ["rollout", "promote", "--to", "assist", "--yes", "--actor", "ops"],
    )

    assert result.exit_code == 0, result.output
    assert "promoted" in result.output.lower()

    # State-file written with the new stage.
    state_path = settings.audit_log_path.parent / "rollout_state.json"
    state = RolloutStateStore(state_path).load()
    assert state is not None
    assert state.stage == "assist"
    assert state.previous_stage == "shadow"
    assert state.promoted_by == "ops"

    # Audit JSONL contains the promote envelope.
    audit_lines = [
        json.loads(line)
        for line in settings.audit_log_path.read_text().splitlines()
        if line.strip()
    ]
    actions = [e["action"] for e in audit_lines]
    assert "rollout_promote.requested" in actions
    assert "rollout_promote.ok" in actions
    ok = next(e for e in audit_lines if e["action"] == "rollout_promote.ok")
    assert ok["actor"] == "human"
    assert ok["resource"] == "rollout"
    assert ok["result"]["from_stage"] == "shadow"
    assert ok["result"]["to_stage"] == "assist"


def test_rollout_promote_declined_exits_2(
    runner: CliRunner,
    _patch_bootstrap: None,
    settings: Any,
) -> None:
    """Without --yes, a "no" answer to the prompt aborts with exit 2."""
    settings.agent_policy_path.parent.mkdir(parents=True, exist_ok=True)
    settings.agent_policy_path.write_text(
        "account_daily_budget_cap_rub: 50000\nrollout_stage: shadow\n"
    )

    # CliRunner.invoke supports ``input=`` for the typer.confirm prompt.
    result = runner.invoke(app, ["rollout", "promote", "--to", "assist"], input="n\n")

    assert result.exit_code == 2, result.output
    assert "abort" in result.output.lower()


def test_rollout_promote_downgrade_allowed(
    runner: CliRunner,
    _patch_bootstrap: None,
    settings: Any,
) -> None:
    """Operators must be able to roll BACK to a tighter stage —
    downgrade is the safety win after an incident."""
    from datetime import UTC, datetime

    from yadirect_agent.rollout import RolloutState, RolloutStateStore

    settings.agent_policy_path.parent.mkdir(parents=True, exist_ok=True)
    settings.agent_policy_path.write_text(
        "account_daily_budget_cap_rub: 50000\nrollout_stage: shadow\n"
    )
    state_path = settings.audit_log_path.parent / "rollout_state.json"
    RolloutStateStore(state_path).save(
        RolloutState(
            stage="autonomy_full",
            promoted_at=datetime(2026, 4, 27, 12, 0, tzinfo=UTC),
            promoted_by="ops",
            previous_stage="autonomy_light",
        )
    )

    result = runner.invoke(app, ["rollout", "promote", "--to", "shadow", "--yes", "--actor", "ops"])

    assert result.exit_code == 0, result.output
    state = RolloutStateStore(state_path).load()
    assert state is not None
    assert state.stage == "shadow"
    assert state.previous_stage == "autonomy_full"


def test_apply_plan_routes_resume_campaigns_to_service(
    runner: CliRunner,
    _patch_bootstrap: None,
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: a stored ``resume_campaigns`` plan flows through the
    CLI router → CampaignService.resume(_applying_plan_id) → DirectService.

    Locks the bulk-args round-trip: ``args={"campaign_ids": [1, 2, 3]}``
    must arrive at ``svc.resume`` as the same list (no truncation, no
    re-ordering, no key-name mismatch).
    """
    from datetime import UTC, datetime

    from yadirect_agent.agent.pipeline import (
        ReviewContext,
        SafetyDecision,
        SafetyPipeline,
        serialize_review_context,
    )
    from yadirect_agent.agent.plans import OperationPlan, PendingPlansStore
    from yadirect_agent.services.campaigns import CampaignService

    plans_path = settings.audit_log_path.parent / "pending_plans.jsonl"
    plan = OperationPlan(
        plan_id="plan-resume",
        created_at=datetime(2026, 4, 27, 12, 0, tzinfo=UTC),
        action="resume_campaigns",
        resource_type="campaign",
        resource_ids=[1, 2, 3],
        args={"campaign_ids": [1, 2, 3]},
        preview="resume campaigns: [1, 2, 3]",
        reason="confirm",
        review_context=serialize_review_context(ReviewContext()),
    )
    PendingPlansStore(plans_path).append(plan)

    def stub_review(self: SafetyPipeline, plan_arg: Any, ctx: Any) -> SafetyDecision:
        return SafetyDecision(status="allow", reason="ok")

    captured: list[tuple[list[int], str | None]] = []

    async def fake_resume(
        self: CampaignService,
        campaign_ids: list[int],
        *,
        _applying_plan_id: str | None = None,
    ) -> None:
        captured.append((list(campaign_ids), _applying_plan_id))

    monkeypatch.setattr(SafetyPipeline, "review", stub_review)
    monkeypatch.setattr(CampaignService, "resume", fake_resume)

    result = runner.invoke(app, ["apply-plan", "plan-resume"])

    assert result.exit_code == 0, result.output
    assert captured == [([1, 2, 3], "plan-resume")]
    final = PendingPlansStore(plans_path).get("plan-resume")
    assert final is not None
    assert final.status == "applied"


# --------------------------------------------------------------------------
# `mcp` subcommand (M3).
# --------------------------------------------------------------------------


def test_mcp_serve_help_shows_allow_write_flag(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``yadirect-agent mcp serve --help`` mentions ``--allow-write``
    so an operator running ``--help`` for the first time discovers
    the gating flag without reading source.

    ``COLUMNS=200`` keeps rich from wrapping the flag name across
    lines in narrow CI terminals (the default 80-column wrap broke
    ``--allow-write`` into ``--allow-\\nwrite`` which made the
    substring assertion flake on Linux CI even though it passed
    locally).
    """
    monkeypatch.setenv("COLUMNS", "200")
    result = runner.invoke(app, ["mcp", "serve", "--help"])
    assert result.exit_code == 0
    assert "--allow-write" in result.output
    assert "MCP_ALLOW_WRITE" in result.output


def test_mcp_serve_invokes_build_mcp_server_with_allow_write_default_false(
    runner: CliRunner,
    _patch_bootstrap: None,
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without ``--allow-write`` and without ``MCP_ALLOW_WRITE`` env,
    the CLI passes ``allow_write=False`` to ``build_mcp_server``.
    Stdio transport is mocked so the test doesn't hang.
    """
    captured: dict[str, Any] = {}

    def fake_build(s: Any, *, allow_write: bool) -> Any:
        captured["allow_write"] = allow_write

        class _Handle:
            class _Server:
                async def run(self, *a: Any, **kw: Any) -> None:
                    return None

                def create_initialization_options(self) -> dict[str, Any]:
                    return {}

            server = _Server()

        return _Handle()

    async def fake_run_stdio(handle: Any) -> None:
        # Mock out stdio binding entirely.
        return None

    from yadirect_agent.cli import main as cli_main

    monkeypatch.setattr(cli_main, "_run_mcp_stdio", fake_run_stdio)
    monkeypatch.setenv("MCP_ALLOW_WRITE", "")

    # Patch the import inside the command body so build_mcp_server stays
    # mocked.
    monkeypatch.setattr("yadirect_agent.mcp.server.build_mcp_server", fake_build)

    result = runner.invoke(app, ["mcp", "serve"])
    assert result.exit_code == 0, result.output
    assert captured["allow_write"] is False


def test_mcp_serve_respects_env_allow_write_true(
    runner: CliRunner,
    _patch_bootstrap: None,
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``MCP_ALLOW_WRITE=true`` env enables write tools when the CLI
    flag isn't passed. Confirms operators can configure the gating
    posture via the deployment env without changing the launch command.
    """
    captured: dict[str, Any] = {}

    def fake_build(s: Any, *, allow_write: bool) -> Any:
        captured["allow_write"] = allow_write

        class _Handle:
            class _Server:
                async def run(self, *a: Any, **kw: Any) -> None:
                    return None

                def create_initialization_options(self) -> dict[str, Any]:
                    return {}

            server = _Server()

        return _Handle()

    async def fake_run_stdio(handle: Any) -> None:
        return None

    from yadirect_agent.cli import main as cli_main

    monkeypatch.setattr(cli_main, "_run_mcp_stdio", fake_run_stdio)
    monkeypatch.setenv("MCP_ALLOW_WRITE", "true")
    monkeypatch.setattr("yadirect_agent.mcp.server.build_mcp_server", fake_build)

    result = runner.invoke(app, ["mcp", "serve"])
    assert result.exit_code == 0
    assert captured["allow_write"] is True
