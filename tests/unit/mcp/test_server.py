"""Tests for the MCP server wrapper (M3.1 + M3.2).

Scope: ``build_mcp_server`` correctly wraps the existing
``ToolRegistry``, gates write tools behind ``allow_write``, and
preserves the safety / audit envelope built by
``build_default_registry``. Stdio transport / Claude Desktop
integration are operator-runnable end-to-end concerns and not
unit-tested here.
"""

from __future__ import annotations

from typing import Any

import pytest

from yadirect_agent.mcp.server import build_mcp_server

_READ_ONLY_TOOLS = {"list_campaigns", "get_keywords", "validate_phrases"}
_GATED_WRITE_TOOLS = {
    "pause_campaigns",
    "resume_campaigns",
    "set_campaign_budget",
}
# ``set_keyword_bids`` is on the MCP write-tool denylist until
# ``BiddingService.apply`` gets ``@requires_plan`` (auditor M3 MEDIUM
# M-2). Until then the tool is NEVER published over MCP, even with
# ``--allow-write``.
_DENYLISTED_WRITE_TOOLS = {"set_keyword_bids"}
_ALL_EXPOSED_WITH_WRITE = _READ_ONLY_TOOLS | _GATED_WRITE_TOOLS


class TestBuildMcpServer:
    def test_read_only_mode_excludes_write_tools(self, settings: Any) -> None:
        """Default (allow_write=False) → write tools are NOT registered.

        The agent in Claude Desktop simply doesn't see them. This is
        defence-in-depth on top of the existing @requires_plan gate;
        even a misconfigured agent_policy.yml that auto-approved
        every action couldn't trigger a mutation through MCP without
        the operator explicitly opting in.
        """
        handle = build_mcp_server(settings, allow_write=False)
        names = {t.name for t in handle.tools}

        assert _READ_ONLY_TOOLS.issubset(names)
        assert names.isdisjoint(_GATED_WRITE_TOOLS)
        assert names.isdisjoint(_DENYLISTED_WRITE_TOOLS)

    def test_allow_write_exposes_all_tools(self, settings: Any) -> None:
        """``--allow-write`` (or ``MCP_ALLOW_WRITE=true``) opts in to
        the full mutating surface. Each write tool still goes through
        the existing safety pipeline + plan→confirm→execute, so a
        Claude Desktop agent calling ``set_campaign_budget`` lands
        the same ``status: pending, plan_id: ...`` response shape
        the CLI's tool handler returns today.
        """
        handle = build_mcp_server(settings, allow_write=True)
        names = {t.name for t in handle.tools}

        # Read-only + the three gated write tools are exposed.
        assert _ALL_EXPOSED_WITH_WRITE.issubset(names)
        # Auditor M3 M-2: denylisted write tools (currently
        # set_keyword_bids) are NEVER published, even with
        # --allow-write, until they get @requires_plan gating.
        assert names.isdisjoint(_DENYLISTED_WRITE_TOOLS)

    def test_tool_input_schemas_match_pydantic_models(self, settings: Any) -> None:
        """Each MCP tool's inputSchema must come from the pydantic
        ``input_model`` of the underlying ``Tool`` — preserves the
        ``extra='forbid'`` posture and Field constraints the agent
        path enforces. A divergence here would let MCP clients
        bypass ``min_length``, ``ge=300``, etc.
        """
        handle = build_mcp_server(settings, allow_write=True)
        tools_by_name = {t.name: t for t in handle.tools}

        # ``set_campaign_budget`` has ``ge=300`` on budget_rub. Verify
        # the schema carries it through.
        scb = tools_by_name["set_campaign_budget"]
        budget_schema = scb.inputSchema["properties"]["budget_rub"]
        assert budget_schema.get("minimum") == 300

        # ``additionalProperties: false`` from extra="forbid" must
        # propagate so Claude Desktop's MCP client rejects unknown
        # fields before they ever reach our handler.
        assert scb.inputSchema.get("additionalProperties") is False

    def test_denylisted_write_tool_never_dispatchable_even_with_allow_write(
        self, settings: Any
    ) -> None:
        """Auditor M3 M-2 regression guard: a write tool on the
        denylist (``set_keyword_bids`` today) MUST refuse to dispatch
        even when the operator passed ``--allow-write``. The tool is
        not registered, so ``dispatch`` looks up ``_exposed`` and
        misses — surfacing as ``ValueError("unknown tool ...")``.
        """
        import asyncio

        handle = build_mcp_server(settings, allow_write=True)
        with pytest.raises(ValueError, match="unknown tool"):
            asyncio.run(handle.dispatch("set_keyword_bids", {"updates": []}))

    def test_tool_descriptions_propagate(self, settings: Any) -> None:
        """The agent in Claude Desktop relies on the ``description``
        text to know how to use a tool. The wrapper must forward the
        description from the underlying ``Tool`` verbatim — a missing
        description here would render the tool unusable to the LLM.
        """
        handle = build_mcp_server(settings, allow_write=True)
        for tool in handle.tools:
            assert tool.description, f"{tool.name} missing description"


class TestMcpToolDispatch:
    @pytest.mark.asyncio
    async def test_list_campaigns_dispatches_through_existing_handler(
        self,
        settings: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """End-to-end: an MCP ``call_tool`` request for
        ``list_campaigns`` flows through to the existing handler in
        the registry and returns the structured response the agent
        path produces today. Pin the contract — a future refactor
        that built a parallel handler chain for MCP would silently
        diverge.
        """
        from yadirect_agent.services.campaigns import (
            CampaignService,
            CampaignSummary,
        )

        async def fake_list_all(self: CampaignService) -> list[CampaignSummary]:
            return [
                CampaignSummary(
                    id=42,
                    name="alpha",
                    state="ON",
                    status="ACCEPTED",
                    type="TEXT_CAMPAIGN",
                    daily_budget_rub=500.0,
                )
            ]

        monkeypatch.setattr(CampaignService, "list_all", fake_list_all)

        handle = build_mcp_server(settings, allow_write=False)
        result = await handle.dispatch("list_campaigns", {})

        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["id"] == 42
        assert result[0]["name"] == "alpha"

    @pytest.mark.asyncio
    async def test_dispatch_unknown_tool_raises_value_error(self, settings: Any) -> None:
        handle = build_mcp_server(settings, allow_write=False)
        with pytest.raises(ValueError, match="unknown tool"):
            await handle.dispatch("does_not_exist", {})

    @pytest.mark.asyncio
    async def test_dispatch_write_tool_in_read_only_mode_raises(self, settings: Any) -> None:
        """Even if a misbehaving MCP client somehow invented a write
        tool name, the read-only-mode handle must refuse to dispatch
        it. The defence is structural (write tools aren't registered
        at all) but the explicit refusal pins the contract.
        """
        handle = build_mcp_server(settings, allow_write=False)
        with pytest.raises(ValueError, match="unknown tool"):
            await handle.dispatch("set_campaign_budget", {"campaign_id": 1, "budget_rub": 500})
