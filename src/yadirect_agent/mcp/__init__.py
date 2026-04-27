"""MCP server adapter for ``yadirect-agent`` (M3).

Exposes the existing ``ToolRegistry`` over the Model Context
Protocol so a Claude Desktop / Claude Code agent can drive the
account through the same handlers the in-process agent loop uses.
The MCP layer is a thin wrapper:

- safety pipeline / plan→confirm→execute / audit are the SAME
  modules the CLI uses; the MCP handler dispatches into the
  shared ``Tool.handler`` of each registered tool.
- write tools are gated behind ``allow_write`` (CLI flag or
  ``MCP_ALLOW_WRITE=true``) — defence in depth on top of the
  per-method ``@requires_plan`` decorator.
- structured tool responses (``{status: ..., ...}``) flow back
  to the LLM verbatim; the MCP runtime serialises them to JSON.

See ``docs/OPERATING.md`` for the operator runbook.
"""
