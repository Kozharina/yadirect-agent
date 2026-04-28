"""Tests for ``yadirect-agent rationale`` CLI subcommand (M20.3 slice)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from typer.testing import CliRunner

from yadirect_agent.agent.rationale_store import RationaleStore
from yadirect_agent.cli.main import app
from yadirect_agent.models.rationale import (
    Alternative,
    Confidence,
    InputDataPoint,
    Rationale,
)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def _patch_bootstrap(monkeypatch: pytest.MonkeyPatch, settings: Any) -> None:
    """Wire bootstrap to the test settings (no real env resolution)."""
    monkeypatch.setattr("yadirect_agent.cli.main.get_settings", lambda: settings)
    monkeypatch.setattr("yadirect_agent.cli.main.configure_logging", lambda _s: None)


def _seed_store(settings: Any, *rationales: Rationale) -> RationaleStore:
    path = settings.audit_log_path.parent / "rationale.jsonl"
    store = RationaleStore(path)
    for r in rationales:
        store.append(r)
    return store


def _make_rationale(
    *,
    decision_id: str = "abc123",
    action: str = "campaigns.set_daily_budget",
    resource_ids: list[int] | None = None,
    summary: str = "lowering budget because CPA crept above target",
    timestamp: datetime | None = None,
    inputs: list[InputDataPoint] | None = None,
    alternatives: list[Alternative] | None = None,
    confidence: Confidence = Confidence.MEDIUM,
) -> Rationale:
    return Rationale(
        decision_id=decision_id,
        action=action,
        resource_type="campaign",
        resource_ids=resource_ids if resource_ids is not None else [42],
        summary=summary,
        timestamp=timestamp or datetime.now(UTC),
        inputs=inputs or [],
        alternatives_considered=alternatives or [],
        confidence=confidence,
    )


# --------------------------------------------------------------------------
# `rationale show` — single record detail.
# --------------------------------------------------------------------------


class TestRationaleShow:
    def test_unknown_id_exits_nonzero(
        self,
        runner: CliRunner,
        _patch_bootstrap: None,
    ) -> None:
        result = runner.invoke(app, ["rationale", "show", "nonexistent"])

        assert result.exit_code != 0
        assert "no rationale" in result.output.lower() or "not found" in result.output.lower()

    def test_known_id_shows_summary_and_metadata(
        self,
        runner: CliRunner,
        settings: Any,
        _patch_bootstrap: None,
    ) -> None:
        observed = datetime(2026, 4, 20, 12, 0, tzinfo=UTC)
        rationale = _make_rationale(
            decision_id="abc123",
            inputs=[
                InputDataPoint(
                    name="cpa_rub_7d",
                    value=850.0,
                    source="metrika",
                    observed_at=observed,
                ),
            ],
            alternatives=[
                Alternative(
                    description="raise bid by 50%",
                    rejected_because="exceeds policy ceiling",
                ),
            ],
            confidence=Confidence.HIGH,
        )
        _seed_store(settings, rationale)

        result = runner.invoke(app, ["rationale", "show", "abc123"])

        assert result.exit_code == 0, result.output
        assert "abc123" in result.output
        assert rationale.summary in result.output
        # Inputs shown
        assert "cpa_rub_7d" in result.output
        assert "metrika" in result.output
        # Alternatives shown
        assert "raise bid by 50%" in result.output
        assert "exceeds policy ceiling" in result.output
        # Confidence shown
        assert "high" in result.output.lower()

    def test_json_mode_emits_valid_json(
        self,
        runner: CliRunner,
        settings: Any,
        _patch_bootstrap: None,
    ) -> None:
        rationale = _make_rationale(decision_id="abc123")
        _seed_store(settings, rationale)

        result = runner.invoke(app, ["rationale", "show", "abc123", "--json"])

        assert result.exit_code == 0, result.output
        import json

        payload = json.loads(result.output)
        assert payload["decision_id"] == "abc123"
        assert payload["confidence"] == "medium"


# --------------------------------------------------------------------------
# `rationale list` — recent rationales, optionally filtered by campaign.
# --------------------------------------------------------------------------


class TestRationaleList:
    def test_empty_store_prints_no_rationales(
        self,
        runner: CliRunner,
        settings: Any,
        _patch_bootstrap: None,
    ) -> None:
        _seed_store(settings)  # no rationales

        result = runner.invoke(app, ["rationale", "list"])

        assert result.exit_code == 0, result.output

    def test_default_window_is_seven_days(
        self,
        runner: CliRunner,
        settings: Any,
        _patch_bootstrap: None,
    ) -> None:
        now = datetime.now(UTC)
        old = _make_rationale(decision_id="old", timestamp=now - timedelta(days=10))
        recent = _make_rationale(decision_id="recent", timestamp=now - timedelta(days=2))
        _seed_store(settings, old, recent)

        result = runner.invoke(app, ["rationale", "list"])

        assert result.exit_code == 0, result.output
        assert "recent" in result.output
        assert "old" not in result.output

    def test_days_option_extends_window(
        self,
        runner: CliRunner,
        settings: Any,
        _patch_bootstrap: None,
    ) -> None:
        now = datetime.now(UTC)
        old = _make_rationale(decision_id="old", timestamp=now - timedelta(days=10))
        _seed_store(settings, old)

        result = runner.invoke(app, ["rationale", "list", "--days", "30"])

        assert result.exit_code == 0, result.output
        assert "old" in result.output

    def test_campaign_filter(
        self,
        runner: CliRunner,
        settings: Any,
        _patch_bootstrap: None,
    ) -> None:
        now = datetime.now(UTC)
        for_42 = _make_rationale(
            decision_id="for_42",
            resource_ids=[42],
            timestamp=now - timedelta(hours=1),
        )
        for_51 = _make_rationale(
            decision_id="for_51",
            resource_ids=[51],
            timestamp=now - timedelta(hours=1),
        )
        _seed_store(settings, for_42, for_51)

        result = runner.invoke(app, ["rationale", "list", "--campaign", "42"])

        assert result.exit_code == 0, result.output
        assert "for_42" in result.output
        assert "for_51" not in result.output

    def test_zero_days_rejected(
        self,
        runner: CliRunner,
        _patch_bootstrap: None,
    ) -> None:
        # Empty-window list is almost always a typo — reject.
        result = runner.invoke(app, ["rationale", "list", "--days", "0"])

        assert result.exit_code != 0


# --------------------------------------------------------------------------
# Markup-injection safety in CLI rendering (mirrors M15.5.1 HIGH-1).
# --------------------------------------------------------------------------


class TestRationaleRendererSafety:
    def test_rich_markup_in_summary_does_not_inject(
        self,
        runner: CliRunner,
        settings: Any,
        _patch_bootstrap: None,
    ) -> None:
        rationale = _make_rationale(
            decision_id="abc123",
            summary="lowering [bold red]PWNED[/bold red] budget",
        )
        _seed_store(settings, rationale)

        result = runner.invoke(app, ["rationale", "show", "abc123"])

        assert result.exit_code == 0
        # Literal bracket sequence preserved (escaped, not interpreted).
        assert "[bold red]" in result.output
