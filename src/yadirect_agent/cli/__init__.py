"""Command-line entry points.

Two roles:
- `yadirect-agent` (this package) — typer app for humans + cron.
- `yadirect-mcp` (src/yadirect_agent/mcp_server) — stdio MCP adapter.

Both share the agent/ core — no tool is duplicated between adapters.
"""
