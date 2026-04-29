"""Tests for ToolRegistry + the seven default tools.

Strategy:
- Registry mechanics (add/get/schemas/dup) are pure and tested directly.
- Per-tool handlers are exercised through a registry built against the test
  `settings` fixture. Services underneath are monkeypatched to return
  fixed shapes; no HTTP.
- Input validation: we rely on pydantic, so we spot-check the handful of
  non-trivial constraints (min_length, ge=300) rather than restating schema.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from yadirect_agent.agent.tools import (
    Tool,
    ToolContext,
    ToolRegistry,
    build_default_registry,
)
from yadirect_agent.config import Settings
from yadirect_agent.models.keywords import Keyword

# --------------------------------------------------------------------------
# Registry mechanics.
# --------------------------------------------------------------------------


class _EmptyInput(BaseModel):
    pass


def _noop_tool(name: str = "noop", *, is_write: bool = False) -> Tool:
    async def handler(_inp: BaseModel, _ctx: ToolContext) -> Any:
        return {"ok": True}

    return Tool(
        name=name,
        description="noop",
        input_model=_EmptyInput,
        is_write=is_write,
        handler=handler,
    )


class TestRegistry:
    def test_add_and_get_roundtrip(self) -> None:
        reg = ToolRegistry()
        reg.add(_noop_tool("alpha"))

        assert "alpha" in reg
        assert reg.get("alpha").name == "alpha"
        assert reg.names() == ["alpha"]
        assert len(reg) == 1

    def test_duplicate_add_is_rejected(self) -> None:
        reg = ToolRegistry()
        reg.add(_noop_tool("alpha"))
        with pytest.raises(ValueError, match="already registered"):
            reg.add(_noop_tool("alpha"))

    def test_get_missing_raises(self) -> None:
        reg = ToolRegistry()
        with pytest.raises(KeyError, match="unknown tool"):
            reg.get("missing")

    def test_schemas_match_anthropic_shape(self) -> None:
        reg = ToolRegistry()
        reg.add(_noop_tool("alpha"))
        schemas = reg.schemas()

        assert len(schemas) == 1
        s = schemas[0]
        assert set(s) == {"name", "description", "input_schema"}
        assert s["name"] == "alpha"
        # input_schema must be a JSON-schema-ish mapping.
        assert isinstance(s["input_schema"], dict)
        assert s["input_schema"].get("type") == "object"


# --------------------------------------------------------------------------
# build_default_registry: shape of the seven-tool set.
# --------------------------------------------------------------------------


class TestDefaultRegistry:
    def test_exposes_seven_named_tools(self, settings: Settings) -> None:
        reg = build_default_registry(settings)

        assert len(reg) == 7
        assert set(reg.names()) == {
            "list_campaigns",
            "pause_campaigns",
            "resume_campaigns",
            "set_campaign_budget",
            "get_keywords",
            "set_keyword_bids",
            "validate_phrases",
        }

    @pytest.mark.parametrize(
        ("name", "is_write"),
        [
            ("list_campaigns", False),
            ("pause_campaigns", True),
            ("resume_campaigns", True),
            ("set_campaign_budget", True),
            ("get_keywords", False),
            ("set_keyword_bids", True),
            ("validate_phrases", False),
        ],
    )
    def test_write_flags_match_spec(self, settings: Settings, name: str, is_write: bool) -> None:
        reg = build_default_registry(settings)
        assert reg.get(name).is_write is is_write

    @pytest.mark.parametrize(
        "tool_name",
        [
            "list_campaigns",
            "pause_campaigns",
            "resume_campaigns",
            "set_campaign_budget",
            "get_keywords",
            "set_keyword_bids",
            "validate_phrases",
        ],
    )
    def test_input_models_reject_unknown_fields(self, settings: Settings, tool_name: str) -> None:
        # Defence-in-depth (auditor HIGH-2 + second-pass LOW-1): the
        # agent must not be able to sneak ``_applying_plan_id`` (or
        # anything else) through the tool input pydantic model.
        # extra="forbid" lives on each model individually — a future
        # refactor that drops it from one model is the regression
        # this parametric sweep catches.
        from pydantic import ValidationError

        reg = build_default_registry(settings)
        tool = reg.get(tool_name)
        with pytest.raises(ValidationError):
            tool.input_model.model_validate({"_probe_unknown_field": "x"})

    @pytest.mark.parametrize(
        ("tool_name", "args"),
        [
            ("pause_campaigns", {"ids": [1]}),
            ("resume_campaigns", {"ids": [1]}),
            ("set_campaign_budget", {"campaign_id": 1, "budget_rub": 500}),
            (
                "set_keyword_bids",
                {"updates": [{"keyword_id": 1, "new_search_bid_rub": 5.0}]},
            ),
        ],
    )
    def test_mutating_tool_inputs_require_reason_field(
        self, settings: Settings, tool_name: str, args: dict[str, Any]
    ) -> None:
        # M20 slice 2: ``reason`` is hard-required on every mutating
        # tool input. The decorator emits a Rationale at decision
        # time using ``inp.reason`` as the summary; a missing reason
        # at the boundary means we cannot honour M20.3 ("the agent
        # retrieves recorded rationale, doesn't fabricate on demand")
        # — so we refuse the tool call up front, BEFORE any safety
        # pipeline runs.
        from pydantic import ValidationError

        reg = build_default_registry(settings)
        tool = reg.get(tool_name)
        with pytest.raises(ValidationError, match="reason"):
            tool.input_model.model_validate(args)

    @pytest.mark.parametrize(
        ("tool_name", "args"),
        [
            ("pause_campaigns", {"ids": [1]}),
            ("resume_campaigns", {"ids": [1]}),
            ("set_campaign_budget", {"campaign_id": 1, "budget_rub": 500}),
            (
                "set_keyword_bids",
                {"updates": [{"keyword_id": 1, "new_search_bid_rub": 5.0}]},
            ),
        ],
    )
    def test_mutating_tool_inputs_reject_short_reason(
        self, settings: Settings, tool_name: str, args: dict[str, Any]
    ) -> None:
        # ``min_length=10`` catches "ok", "yes", "do it" — none of
        # which are useful rationale for shadow-week calibration.
        # The threshold is deliberately low (≈ two short words)
        # because we don't want to force the LLM to artificially
        # pad reasons to hit a higher bar.
        from pydantic import ValidationError

        reg = build_default_registry(settings)
        tool = reg.get(tool_name)
        with pytest.raises(ValidationError, match="at least 10"):
            tool.input_model.model_validate({**args, "reason": "ok"})

    @pytest.mark.parametrize(
        "tool_name",
        ["list_campaigns", "get_keywords", "validate_phrases"],
    )
    def test_read_only_tool_inputs_do_not_require_reason(
        self, settings: Settings, tool_name: str
    ) -> None:
        # Read-only tools have no decision attached, so a reason
        # would be ceremonial. Pin the asymmetry: only mutating
        # tools demand articulated rationale.
        from pydantic import ValidationError

        reg = build_default_registry(settings)
        tool = reg.get(tool_name)
        # The minimum payload differs per read-only tool; instead of
        # spelling each out, assert that passing an unexpected
        # ``reason`` field IS rejected (extra="forbid" still
        # applies). If the field were silently accepted, that would
        # hint a future drift toward also requiring reason on read
        # tools.
        with pytest.raises(ValidationError):
            tool.input_model.model_validate({"reason": "this should be rejected here"})

    def test_build_safety_pair_called_once_per_registry(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # SafetyPipeline.SessionState (cross-tool TOCTOU register) only
        # works if every CampaignService-backed tool sees the same
        # pipeline object. The factories take ``(settings, pipeline,
        # store)`` so they share a closure; this test pins that the
        # registry construction calls ``build_safety_pair`` exactly
        # once. A future refactor that built per-tool pipelines would
        # silently disable session-level TOCTOU protection.
        from yadirect_agent.agent import tools as tools_mod

        call_count = 0
        original = tools_mod.build_safety_pair

        def _counting(s: Settings) -> Any:
            nonlocal call_count
            call_count += 1
            return original(s)

        monkeypatch.setattr(tools_mod, "build_safety_pair", _counting)
        build_default_registry(settings)
        assert call_count == 1


# --------------------------------------------------------------------------
# M20 slice 2 — handlers construct a ``Rationale`` from ``inp.reason``
# and pass it via ``rationale=`` to the underlying service method.
# The decorator overwrites ``decision_id`` with ``plan.plan_id`` and
# persists; the test patches at the service level (so the decorator
# is bypassed) and verifies the kwarg shape directly.
# --------------------------------------------------------------------------


class TestHandlersPassRationaleToService:
    @pytest.mark.parametrize(
        (
            "tool_name",
            "args",
            "service_class_name",
            "method_name",
            "expected_action",
            "expected_rt",
            "expected_ids",
        ),
        [
            (
                "pause_campaigns",
                {"ids": [1, 2], "reason": "CTR < 0.5% over the last 7 days."},
                "CampaignService",
                "pause",
                "pause_campaigns",
                "campaign",
                [1, 2],
            ),
            (
                "resume_campaigns",
                {"ids": [3], "reason": "Refreshed creatives, resuming campaign."},
                "CampaignService",
                "resume",
                "resume_campaigns",
                "campaign",
                [3],
            ),
            (
                "set_campaign_budget",
                {
                    "campaign_id": 5,
                    "budget_rub": 700,
                    "reason": "Strong ROAS this week, scaling spend.",
                },
                "CampaignService",
                "set_daily_budget",
                "set_campaign_budget",
                "campaign",
                [5],
            ),
            (
                "set_keyword_bids",
                {
                    "updates": [{"keyword_id": 9, "new_search_bid_rub": 8.0}],
                    "reason": "Top converter, raising bid by 10%.",
                },
                "BiddingService",
                "apply",
                "set_keyword_bids",
                "keyword",
                [9],
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_handler_constructs_rationale_with_reason_as_summary(
        self,
        settings: Settings,
        tool_context: ToolContext,
        monkeypatch: pytest.MonkeyPatch,
        tool_name: str,
        args: dict[str, Any],
        service_class_name: str,
        method_name: str,
        expected_action: str,
        expected_rt: str,
        expected_ids: list[int],
    ) -> None:
        from yadirect_agent.services.bidding import BiddingService
        from yadirect_agent.services.campaigns import CampaignService

        captured: dict[str, Any] = {}

        async def _fake(self: Any, *_args: Any, **kwargs: Any) -> None:
            captured.update(kwargs)

        target_class = (
            CampaignService if service_class_name == "CampaignService" else BiddingService
        )
        monkeypatch.setattr(target_class, method_name, _fake)

        tool = build_default_registry(settings).get(tool_name)
        inp = tool.input_model.model_validate(args)
        await tool.handler(inp, tool_context)

        # Handler MUST pass ``rationale=`` so the decorator can persist
        # it. Without this, M20 slice 2 collapses to the same
        # ``rationale.missing`` warning the soft-optional path used
        # to emit — the operator still gets no record of the reason.
        assert "rationale" in captured, (
            f"{tool_name} handler did not pass rationale= to {service_class_name}.{method_name}"
        )
        rat = captured["rationale"]
        # Summary IS the reason verbatim — the args are already in
        # ``plan.preview`` / ``plan.args``, no need to duplicate them
        # into the summary.
        assert rat.summary == args["reason"]
        # Action / resource_type / resource_ids mirror the
        # ``@requires_plan(...)`` configuration so a future read-back
        # query like ``rationale list --action=set_campaign_budget``
        # finds this record.
        assert rat.action == expected_action
        assert rat.resource_type == expected_rt
        assert rat.resource_ids == expected_ids


# --------------------------------------------------------------------------
# M2.4 — Daily-budget hard guard (env backstop).
#
# When ``settings.agent_max_daily_budget_rub`` is tighter than the YAML's
# ``account_daily_budget_cap_rub``, ``build_safety_pair`` MUST tighten the
# effective Policy so the env wins. Operators set the env at deployment
# time; the YAML can drift (typo / copy-paste / stale checkout); the env
# is the last line of defence.
# --------------------------------------------------------------------------


class TestEnvBackstop:
    def test_env_tighter_than_yaml_wins(self, tmp_path: Path) -> None:
        """``min(yaml, env)`` chooses the smaller cap."""
        from pydantic import SecretStr

        from yadirect_agent.agent.tools import build_safety_pair
        from yadirect_agent.config import Settings

        # Write a YAML with a generous cap.
        yaml_path = tmp_path / "agent_policy.yml"
        yaml_path.write_text("account_daily_budget_cap_rub: 100000\n")
        settings = Settings(
            yandex_direct_token=SecretStr("test-direct-token"),
            yandex_metrika_token=SecretStr("test-metrika-token"),
            yandex_client_login=None,
            yandex_use_sandbox=True,
            anthropic_api_key=SecretStr("test-anthropic-key"),
            anthropic_model="claude-opus-4-7",
            agent_policy_path=yaml_path,
            agent_max_daily_budget_rub=5_000,  # tighter than YAML
            log_level="INFO",
            log_format="json",
            audit_log_path=tmp_path / "logs" / "audit.jsonl",
        )

        pipeline, _, _ = build_safety_pair(settings)
        # Env beats YAML.
        assert pipeline.policy.budget_cap.account_daily_budget_cap_rub == 5_000

    def test_yaml_tighter_than_env_wins(self, tmp_path: Path) -> None:
        """If the YAML is tighter, env doesn't loosen it."""
        from pydantic import SecretStr

        from yadirect_agent.agent.tools import build_safety_pair
        from yadirect_agent.config import Settings

        yaml_path = tmp_path / "agent_policy.yml"
        yaml_path.write_text("account_daily_budget_cap_rub: 3000\n")
        settings = Settings(
            yandex_direct_token=SecretStr("test-direct-token"),
            yandex_metrika_token=SecretStr("test-metrika-token"),
            yandex_client_login=None,
            yandex_use_sandbox=True,
            anthropic_api_key=SecretStr("test-anthropic-key"),
            anthropic_model="claude-opus-4-7",
            agent_policy_path=yaml_path,
            agent_max_daily_budget_rub=10_000,
            log_level="INFO",
            log_format="json",
            audit_log_path=tmp_path / "logs" / "audit.jsonl",
        )

        pipeline, _, _ = build_safety_pair(settings)
        # YAML wins.
        assert pipeline.policy.budget_cap.account_daily_budget_cap_rub == 3_000

    def test_equal_caps_no_change(self, tmp_path: Path) -> None:
        from pydantic import SecretStr

        from yadirect_agent.agent.tools import build_safety_pair
        from yadirect_agent.config import Settings

        yaml_path = tmp_path / "agent_policy.yml"
        yaml_path.write_text("account_daily_budget_cap_rub: 7500\n")
        settings = Settings(
            yandex_direct_token=SecretStr("test-direct-token"),
            yandex_metrika_token=SecretStr("test-metrika-token"),
            yandex_client_login=None,
            yandex_use_sandbox=True,
            anthropic_api_key=SecretStr("test-anthropic-key"),
            anthropic_model="claude-opus-4-7",
            agent_policy_path=yaml_path,
            agent_max_daily_budget_rub=7_500,
            log_level="INFO",
            log_format="json",
            audit_log_path=tmp_path / "logs" / "audit.jsonl",
        )

        pipeline, _, _ = build_safety_pair(settings)
        assert pipeline.policy.budget_cap.account_daily_budget_cap_rub == 7_500

    def test_settings_rejects_negative_env_cap(self) -> None:
        """Auditor M-1: a negative ``AGENT_MAX_DAILY_BUDGET_RUB``
        used to silently propagate through ``min(yaml, env)`` into a
        negative Policy cap that KS#1 then interpreted as "always
        exceeded" — agent frozen with a misleading "cap exceeded"
        error from boot. The Settings field now has ``Field(ge=1)``
        so the typo trap fails fast at parse time.
        """
        from pydantic import SecretStr, ValidationError

        from yadirect_agent.config import Settings

        with pytest.raises(ValidationError):
            Settings(
                yandex_direct_token=SecretStr("x"),
                yandex_metrika_token=SecretStr("x"),
                anthropic_api_key=SecretStr("x"),
                anthropic_model="claude-opus-4-7",
                agent_max_daily_budget_rub=-1,
            )

    def test_rollout_state_override_takes_precedence_over_yaml(self, tmp_path: Path) -> None:
        """When ``rollout_state.json`` exists at the configured path,
        its ``stage`` must override the YAML's ``rollout_stage``.

        This is how ``yadirect-agent rollout promote`` takes effect
        without rewriting the policy YAML by hand: the operator
        promotes once, the state-file is written, every subsequent
        agent run picks up the new stage at boot.
        """
        from datetime import UTC, datetime

        from pydantic import SecretStr

        from yadirect_agent.agent.tools import build_safety_pair
        from yadirect_agent.config import Settings
        from yadirect_agent.rollout import RolloutState, RolloutStateStore

        yaml_path = tmp_path / "agent_policy.yml"
        # YAML says shadow (the safe default).
        yaml_path.write_text("account_daily_budget_cap_rub: 50000\nrollout_stage: shadow\n")
        settings = Settings(
            yandex_direct_token=SecretStr("x"),
            yandex_metrika_token=SecretStr("x"),
            yandex_client_login=None,
            yandex_use_sandbox=True,
            anthropic_api_key=SecretStr("x"),
            anthropic_model="claude-opus-4-7",
            agent_policy_path=yaml_path,
            agent_max_daily_budget_rub=10_000,
            log_level="INFO",
            log_format="json",
            audit_log_path=tmp_path / "logs" / "audit.jsonl",
        )
        # Operator promoted to assist via the CLI; state-file is now
        # next to the audit log (the convention build_safety_pair uses).
        store_path = settings.audit_log_path.parent / "rollout_state.json"
        RolloutStateStore(store_path).save(
            RolloutState(
                stage="assist",
                promoted_at=datetime(2026, 4, 27, 12, 0, tzinfo=UTC),
                promoted_by="ops",
                previous_stage="shadow",
            )
        )

        pipeline, _, _ = build_safety_pair(settings)

        assert pipeline.policy.rollout_stage == "assist"

    def test_yaml_stage_used_when_state_file_missing(self, tmp_path: Path) -> None:
        """No state-file → YAML wins. Fresh deployments stay on the
        configured default until the operator runs ``promote``.
        """
        from pydantic import SecretStr

        from yadirect_agent.agent.tools import build_safety_pair
        from yadirect_agent.config import Settings

        yaml_path = tmp_path / "agent_policy.yml"
        yaml_path.write_text("account_daily_budget_cap_rub: 50000\nrollout_stage: assist\n")
        settings = Settings(
            yandex_direct_token=SecretStr("x"),
            yandex_metrika_token=SecretStr("x"),
            yandex_client_login=None,
            yandex_use_sandbox=True,
            anthropic_api_key=SecretStr("x"),
            anthropic_model="claude-opus-4-7",
            agent_policy_path=yaml_path,
            agent_max_daily_budget_rub=10_000,
            log_level="INFO",
            log_format="json",
            audit_log_path=tmp_path / "logs" / "audit.jsonl",
        )

        pipeline, _, _ = build_safety_pair(settings)
        assert pipeline.policy.rollout_stage == "assist"

    def test_settings_rejects_zero_env_cap(self) -> None:
        """The "freeze the agent" use case is correctly expressed via
        ``rollout_stage="shadow"`` in the policy YAML, not via a
        budget cap of zero (which produces a generic "cap exceeded"
        rejection on every mutation, indistinguishable from a real
        cap violation). ``ge=1`` rejects the anti-pattern.
        """
        from pydantic import SecretStr, ValidationError

        from yadirect_agent.config import Settings

        with pytest.raises(ValidationError):
            Settings(
                yandex_direct_token=SecretStr("x"),
                yandex_metrika_token=SecretStr("x"),
                anthropic_api_key=SecretStr("x"),
                anthropic_model="claude-opus-4-7",
                agent_max_daily_budget_rub=0,
            )

    def test_env_backstop_logged_when_tightening(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Operators MUST see a structured log line when the env-
        backstop kicks in — silent tightening would be a "why is the
        agent rejecting valid budgets" debugging trap.

        We capture via a structlog logger stub since the project's
        ``configure_logging`` may route to JSON / console / stdlib
        depending on settings, and ``caplog`` only sees stdlib
        records reliably.
        """
        from pydantic import SecretStr

        from yadirect_agent.agent import tools as tools_mod
        from yadirect_agent.config import Settings

        yaml_path = tmp_path / "agent_policy.yml"
        yaml_path.write_text("account_daily_budget_cap_rub: 100000\n")
        settings = Settings(
            yandex_direct_token=SecretStr("test-direct-token"),
            yandex_metrika_token=SecretStr("test-metrika-token"),
            yandex_client_login=None,
            yandex_use_sandbox=True,
            anthropic_api_key=SecretStr("test-anthropic-key"),
            anthropic_model="claude-opus-4-7",
            agent_policy_path=yaml_path,
            agent_max_daily_budget_rub=5_000,
            log_level="INFO",
            log_format="json",
            audit_log_path=tmp_path / "logs" / "audit.jsonl",
        )

        warnings: list[tuple[str, dict[str, Any]]] = []
        infos: list[tuple[str, dict[str, Any]]] = []

        class _StubLogger:
            def warning(self, event: str, **kwargs: Any) -> None:
                warnings.append((event, kwargs))

            def info(self, event: str, **kwargs: Any) -> None:
                infos.append((event, kwargs))

        monkeypatch.setattr(tools_mod.structlog, "get_logger", lambda *args, **kw: _StubLogger())

        tools_mod.build_safety_pair(settings)

        events = [event for event, _ in warnings]
        assert "env_backstop_tightening_account_cap" in events
        # The logged kwargs include the explicit caps so an operator
        # can grep for the threshold values.
        idx = events.index("env_backstop_tightening_account_cap")
        kwargs = warnings[idx][1]
        assert kwargs["yaml_cap_rub"] == 100_000
        assert kwargs["env_cap_rub"] == 5_000
        assert kwargs["effective_cap_rub"] == 5_000


# --------------------------------------------------------------------------
# Per-tool handlers — dispatched against monkeypatched services.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_campaigns_default_returns_all(
    settings: Settings,
    tool_context: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from yadirect_agent.services.campaigns import CampaignService

    async def fake_list_all(self: CampaignService, _limit: int = 500) -> list:
        return []

    async def fake_list_active(self: CampaignService, limit: int = 200) -> list:
        raise AssertionError("should not be called when states=None")

    monkeypatch.setattr(CampaignService, "list_all", fake_list_all)
    monkeypatch.setattr(CampaignService, "list_active", fake_list_active)

    tool = build_default_registry(settings).get("list_campaigns")
    result = await tool.handler(tool.input_model(), tool_context)

    assert result == []


@pytest.mark.asyncio
async def test_list_campaigns_with_states_uses_list_active(
    settings: Settings,
    tool_context: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from yadirect_agent.services.campaigns import CampaignService

    async def fake_list_active(self: CampaignService, limit: int = 200) -> list:
        return []

    monkeypatch.setattr(CampaignService, "list_active", fake_list_active)

    tool = build_default_registry(settings).get("list_campaigns")
    inp = tool.input_model.model_validate({"states": ["ON"]})
    result = await tool.handler(inp, tool_context)

    assert result == []


@pytest.mark.asyncio
async def test_pause_campaigns_delegates_and_echoes_ids(
    settings: Settings,
    tool_context: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from yadirect_agent.services.campaigns import CampaignService

    captured: list[list[int]] = []

    async def fake_pause(self: CampaignService, ids: list[int], **_: Any) -> None:
        captured.append(list(ids))

    monkeypatch.setattr(CampaignService, "pause", fake_pause)

    tool = build_default_registry(settings).get("pause_campaigns")
    inp = tool.input_model.model_validate(
        {"ids": [1, 2], "reason": "CTR below 0.5% over the last 7 days."}
    )
    result = await tool.handler(inp, tool_context)

    assert captured == [[1, 2]]
    # Status field added when pause was wired through @requires_plan —
    # handler now always includes it.
    assert result == {"status": "applied", "paused": [1, 2]}


@pytest.mark.asyncio
async def test_resume_campaigns_delegates_and_echoes_ids(
    settings: Settings,
    tool_context: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from yadirect_agent.services.campaigns import CampaignService

    async def fake_resume(self: CampaignService, ids: list[int], **_: Any) -> None:
        return None

    monkeypatch.setattr(CampaignService, "resume", fake_resume)

    tool = build_default_registry(settings).get("resume_campaigns")
    inp = tool.input_model.model_validate(
        {"ids": [7], "reason": "Manually un-paused after creative refresh."}
    )
    result = await tool.handler(inp, tool_context)

    assert result == {"status": "applied", "resumed": [7]}


def test_set_campaign_budget_rejects_below_minimum(settings: Settings) -> None:
    tool = build_default_registry(settings).get("set_campaign_budget")
    with pytest.raises(ValidationError, match="budget_rub"):
        tool.input_model.model_validate(
            {
                "campaign_id": 1,
                "budget_rub": 299,
                "reason": "Lower budget after weak ROI analysis.",
            }
        )


@pytest.mark.asyncio
async def test_set_campaign_budget_passes_through(
    settings: Settings,
    tool_context: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from yadirect_agent.services.campaigns import CampaignService

    captured: list[tuple[int, int]] = []

    async def fake_set_budget(
        self: CampaignService, campaign_id: int, budget_rub: int, **_: Any
    ) -> None:
        captured.append((campaign_id, budget_rub))

    monkeypatch.setattr(CampaignService, "set_daily_budget", fake_set_budget)

    tool = build_default_registry(settings).get("set_campaign_budget")
    inp = tool.input_model.model_validate(
        {
            "campaign_id": 42,
            "budget_rub": 500,
            "reason": "Increase budget after CPA stayed below target for 5 days.",
        }
    )
    result = await tool.handler(inp, tool_context)

    # Status field added in M2.2 part 3b1: handlers now distinguish
    # applied / pending / rejected so the agent can relay the next
    # step to the user.
    assert captured == [(42, 500)]
    assert result == {"status": "applied", "campaign_id": 42, "budget_rub": 500}


@pytest.mark.asyncio
async def test_set_campaign_budget_returns_pending_on_plan_required(
    settings: Settings,
    tool_context: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pipeline ``confirm`` path: handler must surface plan_id + the
    operator's next step, not raise. The agent uses this response to
    tell the user how to approve.
    """
    from yadirect_agent.agent.executor import PlanRequired
    from yadirect_agent.services.campaigns import CampaignService

    async def fake_set_budget(
        self: CampaignService, campaign_id: int, budget_rub: int, **_: Any
    ) -> None:
        raise PlanRequired(
            plan_id="abc123",
            preview="set daily budget on campaign 42 to 800 RUB",
            reason="awaiting operator confirmation",
        )

    monkeypatch.setattr(CampaignService, "set_daily_budget", fake_set_budget)

    tool = build_default_registry(settings).get("set_campaign_budget")
    inp = tool.input_model.model_validate(
        {
            "campaign_id": 42,
            "budget_rub": 800,
            "reason": "Plan to scale spend after positive shadow-week signal.",
        }
    )
    result = await tool.handler(inp, tool_context)

    assert result["status"] == "pending"
    assert result["plan_id"] == "abc123"
    assert "campaign 42" in result["preview"]
    assert "apply-plan abc123" in result["next_step"]


@pytest.mark.asyncio
async def test_set_campaign_budget_returns_rejected_on_plan_rejected(
    settings: Settings,
    tool_context: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pipeline ``reject`` path: handler must surface the reason +
    blocking checks; agent relays to user without leaking internals.
    """
    from yadirect_agent.agent.executor import PlanRejected
    from yadirect_agent.agent.safety import CheckResult
    from yadirect_agent.services.campaigns import CampaignService

    async def fake_set_budget(
        self: CampaignService, campaign_id: int, budget_rub: int, **_: Any
    ) -> None:
        raise PlanRejected(
            reason="exceeds account cap",
            blocking=[CheckResult(status="blocked", reason="budget_cap: account total > 100000")],
        )

    monkeypatch.setattr(CampaignService, "set_daily_budget", fake_set_budget)

    tool = build_default_registry(settings).get("set_campaign_budget")
    inp = tool.input_model.model_validate(
        {
            "campaign_id": 42,
            "budget_rub": 800,
            "reason": "Increase budget under positive ROAS trend.",
        }
    )
    result = await tool.handler(inp, tool_context)

    assert result["status"] == "rejected"
    assert "cap" in result["reason"]
    assert len(result["blocking"]) == 1
    assert result["blocking"][0]["status"] == "blocked"
    assert "budget_cap" in result["blocking"][0]["reason"]


@pytest.mark.asyncio
async def test_set_campaign_budget_redacts_private_keys_from_blocking(
    settings: Settings,
    tool_context: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auditor second-pass MEDIUM: KS#7 (query drift) populates
    ``CheckResult.details["new_queries_sample"]`` with raw user search
    queries. Those terms can contain names, addresses, medical phrases.
    The handler MUST strip the key before returning to the LLM agent
    so the raw queries never reach API-provider retention.
    """
    from yadirect_agent.agent.executor import PlanRejected
    from yadirect_agent.agent.safety import CheckResult
    from yadirect_agent.services.campaigns import CampaignService

    async def fake_set_budget(
        self: CampaignService, campaign_id: int, budget_rub: int, **_: Any
    ) -> None:
        raise PlanRejected(
            reason="query drift exceeds threshold",
            blocking=[
                CheckResult(
                    status="blocked",
                    reason="query_drift: 0.7 > 0.4",
                    details={
                        "new_queries_sample": [
                            "Иванов Иван Иванович телефон",
                            "клиника на Тверской 8 запись",
                        ],
                        "new_share": 0.7,
                        "max_new_share": 0.4,
                    },
                )
            ],
        )

    monkeypatch.setattr(CampaignService, "set_daily_budget", fake_set_budget)

    tool = build_default_registry(settings).get("set_campaign_budget")
    inp = tool.input_model.model_validate(
        {
            "campaign_id": 42,
            "budget_rub": 800,
            "reason": "Increase budget on growing query mix.",
        }
    )
    result = await tool.handler(inp, tool_context)

    blocking_details = result["blocking"][0]["details"]
    # Numerical / non-PII details must remain — the agent uses them.
    assert blocking_details["new_share"] == 0.7
    assert blocking_details["max_new_share"] == 0.4
    # The raw queries MUST be gone.
    assert "new_queries_sample" not in blocking_details


@pytest.mark.asyncio
async def test_resume_campaigns_redacts_ks3_missing_phrases_from_blocking(
    settings: Settings,
    tool_context: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auditor M2-ks3-negatives HIGH-1: KS#3
    (negative-keyword floor) blocks with ``details["missing"]``
    carrying the operator-supplied required phrases the campaign
    lacks. Those phrases are commercial intent — competitor names,
    brand misspells, regulated-product filters — and have no business
    reaching the LLM agent's tool response or any API-provider
    retention. The audit sink already strips ``missing`` via
    ``_PRIVATE_KEYS``; the tool-layer ``_redact_details`` must do
    the same so the agent-facing channel matches the audit-facing
    channel.

    Pre-PR this leak was theoretical because
    ``CampaignBudget.negative_keywords`` was always empty so KS#3
    always returned an empty ``missing`` list. This PR populates
    real negatives, which means real phrases can land in the
    rejected-response details.
    """
    from yadirect_agent.agent.executor import PlanRejected
    from yadirect_agent.agent.safety import CheckResult
    from yadirect_agent.services.campaigns import CampaignService

    async def fake_resume(self: CampaignService, campaign_ids: list[int], **_: Any) -> None:
        raise PlanRejected(
            reason="negative_keyword_floor failed",
            blocking=[
                CheckResult(
                    status="blocked",
                    reason="campaign 42 is missing 2 required negative keyword(s)",
                    details={
                        "campaign_id": 42,
                        "missing": ["competitor_brand", "regulated_phrase"],
                    },
                )
            ],
        )

    monkeypatch.setattr(CampaignService, "resume", fake_resume)

    tool = build_default_registry(settings).get("resume_campaigns")
    inp = tool.input_model.model_validate(
        {"ids": [42], "reason": "Resuming after creative refresh and budget bump."}
    )
    result = await tool.handler(inp, tool_context)

    blocking_details = result["blocking"][0]["details"]
    # Non-PII details must remain — the agent needs to know which
    # campaign tripped the check.
    assert blocking_details["campaign_id"] == 42
    # Operator-supplied phrases MUST be gone.
    assert "missing" not in blocking_details


@pytest.mark.asyncio
async def test_get_keywords_returns_model_dumps(
    settings: Settings,
    tool_context: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from yadirect_agent.clients.direct import DirectService

    async def fake_aenter(self: DirectService) -> DirectService:
        return self

    async def fake_aexit(self: DirectService, *exc_info: object) -> None:
        return None

    async def fake_get_keywords(
        self: DirectService, adgroup_ids: list[int], limit: int = 10_000
    ) -> list[Keyword]:
        return [Keyword(Id=1, AdGroupId=10, Keyword="купить обувь", State="ON", Status="ACCEPTED")]

    monkeypatch.setattr(DirectService, "__aenter__", fake_aenter)
    monkeypatch.setattr(DirectService, "__aexit__", fake_aexit)
    monkeypatch.setattr(DirectService, "get_keywords", fake_get_keywords)

    tool = build_default_registry(settings).get("get_keywords")
    inp = tool.input_model.model_validate({"adgroup_ids": [10]})
    result = await tool.handler(inp, tool_context)

    assert isinstance(result, list)
    assert result[0]["keyword"] == "купить обувь"
    assert result[0]["id"] == 1


def test_get_keywords_requires_nonempty_adgroup_ids(settings: Settings) -> None:
    tool = build_default_registry(settings).get("get_keywords")
    with pytest.raises(ValidationError):
        tool.input_model.model_validate({"adgroup_ids": []})


@pytest.mark.asyncio
async def test_set_keyword_bids_converts_and_forwards(
    settings: Settings,
    tool_context: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from yadirect_agent.services.bidding import BiddingService, BidUpdate

    captured: list[list[BidUpdate]] = []

    async def fake_apply(self: BiddingService, updates: list[BidUpdate], **_: Any) -> None:
        captured.append(list(updates))

    monkeypatch.setattr(BiddingService, "apply", fake_apply)

    tool = build_default_registry(settings).get("set_keyword_bids")
    inp = tool.input_model.model_validate(
        {
            "updates": [{"keyword_id": 1, "new_search_bid_rub": 10.0}],
            "reason": "Raise bid on top-converting keyword.",
        }
    )
    result = await tool.handler(inp, tool_context)

    assert len(captured) == 1
    assert captured[0][0].keyword_id == 1
    assert captured[0][0].new_search_bid_rub == 10.0
    # ``status: applied`` added when set_keyword_bids was wired
    # through @requires_plan in the M2 follow-up.
    assert result == {"status": "applied", "updated": [1]}


@pytest.mark.asyncio
async def test_validate_phrases_maps_presence(
    settings: Settings,
    tool_context: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from yadirect_agent.clients.wordstat import DirectKeywordsResearch

    async def fake_has_search_volume(
        self: DirectKeywordsResearch,
        phrases: list[str],
        geo: list[int] | None = None,
    ) -> dict[str, bool]:
        return {p: i % 2 == 0 for i, p in enumerate(phrases)}

    monkeypatch.setattr(DirectKeywordsResearch, "has_search_volume", fake_has_search_volume)

    tool = build_default_registry(settings).get("validate_phrases")
    inp = tool.input_model.model_validate({"phrases": ["a", "b", "c"]})
    result = await tool.handler(inp, tool_context)

    assert result == {"a": True, "b": False, "c": True}


def test_validate_phrases_requires_nonempty_phrases(settings: Settings) -> None:
    tool = build_default_registry(settings).get("validate_phrases")
    with pytest.raises(ValidationError):
        tool.input_model.model_validate({"phrases": []})


# --------------------------------------------------------------------------
# list_campaigns output shape sanity-check — services may evolve, but the
# tool contract stays the same (flat dict of CampaignSummary fields).
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_campaigns_summary_shape(
    settings: Settings,
    tool_context: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The service wraps `Campaign` in `CampaignSummary`. The tool converts
    # that summary to a dict. We bypass `DirectService` by patching the
    # service method to produce summaries directly.
    from yadirect_agent.services.campaigns import CampaignService, CampaignSummary

    async def fake_summaries(self: CampaignService, _limit: int = 500) -> list[CampaignSummary]:
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

    monkeypatch.setattr(CampaignService, "list_all", fake_summaries)

    tool = build_default_registry(settings).get("list_campaigns")
    result = await tool.handler(tool.input_model(), tool_context)

    assert result == [
        {
            "id": 1,
            "name": "alpha",
            "state": "ON",
            "status": "ACCEPTED",
            "type": "TEXT_CAMPAIGN",
            "daily_budget_rub": 500.0,
        }
    ]
