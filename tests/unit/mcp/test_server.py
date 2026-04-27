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
_WRITE_TOOLS = {
    "pause_campaigns",
    "resume_campaigns",
    "set_campaign_budget",
    "set_keyword_bids",
}
_ALL_TOOLS = _READ_ONLY_TOOLS | _WRITE_TOOLS


class TestBuildMcpServer:
    @pytest.mark.asyncio
    async def test_read_only_mode_excludes_write_tools(self, settings: Any) -> None:
        """Default (allow_write=False) → write tools are NOT registered.

        The agent in Claude Desktop simply doesn't see them. This is
        defence-in-depth on top of the existing @requires_plan gate;
        even a misconfigured agent_policy.yml that auto-approved
        every action couldn't trigger a mutation through MCP without
        the operator explicitly opting in.
        """
        server = build_mcp_server(settings, allow_write=False)
        names = {t.name for t in await server.list_tools()}

        assert _READ_ONLY_TOOLS.issubset(names)
        assert names.isdisjoint(_WRITE_TOOLS)

    @pytest.mark.asyncio
    async def test_allow_write_exposes_all_tools(self, settings: Any) -> None:
        """``--allow-write`` (or ``MCP_ALLOW_WRITE=true``) opts in to
        the full mutating surface. Each write tool still goes through
        the existing safety pipeline + plan→confirm→execute, so a
        Claude Desktop agent calling ``set_campaign_budget`` lands
        the same ``status: pending, plan_id: ...`` response shape
        the CLI's tool handler returns today.
        """
        server = build_mcp_server(settings, allow_write=True)
        names = {t.name for t in await server.list_tools()}

        assert _ALL_TOOLS.issubset(names)

    @pytest.mark.asyncio
    async def test_tool_input_schemas_match_pydantic_models(self, settings: Any) -> None:
        """Each MCP tool's inputSchema must come from the pydantic
        ``input_model`` of the underlying ``Tool`` — preserves the
        ``extra='forbid'`` posture and Field constraints the agent
        path enforces. A divergence here would let MCP clients
        bypass ``min_length``, ``ge=300``, etc.
        """
        server = build_mcp_server(settings, allow_write=True)
        tools_by_name = {t.name: t for t in await server.list_tools()}

        # ``set_campaign_budget`` has ``ge=300`` on budget_rub. Verify
        # the schema carries it through.
        scb = tools_by_name["set_campaign_budget"]
        budget_schema = scb.inputSchema["properties"]["budget_rub"]
        assert budget_schema.get("minimum") == 300

        # ``additionalProperties: false`` from extra="forbid" must
        # propagate so Claude Desktop's MCP client rejects unknown
        # fields before they ever reach our handler.
        assert scb.inputSchema.get("additionalProperties") is False
