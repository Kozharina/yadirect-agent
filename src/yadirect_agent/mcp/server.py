"""``build_mcp_server`` — bridges ``ToolRegistry`` over MCP.

The MCP server side of yadirect-agent is intentionally thin:

1. ``build_default_registry(settings)`` constructs the same
   pipeline / store / audit_sink trio + 7 tool handlers that the
   in-process agent loop and the CLI use. Every safety guarantee
   (forbidden_operations / rollout_stage / KS#1-7 / @requires_plan
   / audit) is already baked into those handlers.
2. We filter out ``is_write=True`` tools when ``allow_write=False``
   so the LLM in Claude Desktop literally cannot see them. Defence
   in depth — even a misconfigured policy that auto-approved every
   action couldn't trigger a mutation through MCP without the
   operator explicitly opting in.
3. For each surviving tool we publish an ``mcp.types.Tool`` whose
   ``inputSchema`` is the pydantic ``input_model``'s JSON Schema
   verbatim. ``extra="forbid"`` becomes
   ``additionalProperties: false`` — the MCP client rejects
   unknown kwargs before they reach our handler.
4. ``call_tool`` requests are routed back through the original
   handler with a fresh ``ToolContext`` (trace_id from the request
   id). Structured responses (``{status: ...}``) go back to the
   LLM verbatim; PlanRequired / PlanRejected exceptions never
   escape the handler — they're already caught and converted to
   the structured form there.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import mcp.types as mcp_types
import structlog
from mcp.server.lowlevel import Server

from ..agent.tools import Tool, ToolContext, ToolRegistry, build_default_registry
from ..config import Settings


@dataclass
class McpServerHandle:
    """A constructed MCP server plus the metadata tests / operators
    inspect.

    - ``server`` is the underlying ``mcp.server.lowlevel.Server``;
      operator code calls ``run_stdio_async`` on it.
    - ``tools`` is the published ``mcp.types.Tool`` list — what
      the LLM sees in Claude Desktop's tool catalogue.
    - ``dispatch(name, args)`` is the same call path the MCP
      runtime uses; tests exercise it directly without spinning up
      an asyncio transport.
    """

    server: Server
    tools: list[mcp_types.Tool]
    _registry: ToolRegistry
    _exposed: dict[str, Tool]

    async def dispatch(self, name: str, args: dict[str, Any]) -> Any:
        """Look up a published tool by name and call its handler.

        Raises ``ValueError("unknown tool ...")`` if the tool is not
        in the exposed set — covers both genuinely unknown names and
        write-tool names sent to a read-only handle.
        """

        tool = self._exposed.get(name)
        if tool is None:
            msg = f"unknown tool: {name!r}"
            raise ValueError(msg)
        inp = tool.input_model.model_validate(args)
        ctx = ToolContext(
            trace_id=str(uuid.uuid4()),
            logger=structlog.get_logger().bind(component="mcp", tool=name),
        )
        return await tool.handler(inp, ctx)


def build_mcp_server(settings: Settings, *, allow_write: bool) -> McpServerHandle:
    """Construct the MCP server façade.

    ``allow_write=False`` (default) keeps mutating tools out of the
    published catalogue entirely — the LLM literally cannot call
    them. ``allow_write=True`` exposes the full set; mutations still
    flow through ``@requires_plan`` and return
    ``{status: "pending", plan_id: ...}`` requiring an
    out-of-band ``yadirect-agent apply-plan <id>`` from the
    operator's terminal.
    """

    registry = build_default_registry(settings)

    exposed: dict[str, Tool] = {}
    published: list[mcp_types.Tool] = []
    for tool in registry:
        if tool.is_write and not allow_write:
            continue
        exposed[tool.name] = tool
        published.append(
            mcp_types.Tool(
                name=tool.name,
                description=tool.description,
                inputSchema=tool.input_model.model_json_schema(),
            )
        )

    server: Server = Server("yadirect-agent")

    # MCP SDK doesn't ship type stubs for these decorator factories.
    # The per-line ``# type: ignore`` covers untyped-decorator
    # (in-venv mypy) and misc (pre-commit mypy mirror) — the two
    # versions surface the same gap under different error codes.
    @server.list_tools()  # type: ignore
    async def _list_tools() -> list[mcp_types.Tool]:
        return list(published)

    @server.call_tool()  # type: ignore
    async def _call_tool(name: str, arguments: dict[str, Any]) -> list[mcp_types.TextContent]:
        # Route through the same handler the in-process agent uses.
        # The handler returns a JSON-serialisable structured dict;
        # MCP wraps it as a single TextContent block.
        import json

        tool = exposed.get(name)
        if tool is None:
            msg = f"unknown tool: {name!r}"
            raise ValueError(msg)

        inp = tool.input_model.model_validate(arguments)
        ctx = ToolContext(
            trace_id=str(uuid.uuid4()),
            logger=structlog.get_logger().bind(component="mcp", tool=name),
        )
        result = await tool.handler(inp, ctx)
        return [
            mcp_types.TextContent(
                type="text",
                text=json.dumps(result, ensure_ascii=False, default=str),
            )
        ]

    return McpServerHandle(
        server=server,
        tools=published,
        _registry=registry,
        _exposed=exposed,
    )
