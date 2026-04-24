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
