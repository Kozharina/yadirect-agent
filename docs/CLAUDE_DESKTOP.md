# Claude Desktop / Claude Code integration via MCP

`yadirect-agent` exposes its seven tools over the [Model Context
Protocol](https://modelcontextprotocol.io) so any MCP-compatible
client — Claude Desktop, Claude Code, [mcp-cli][cli], custom
agents — can drive the account.

The MCP server reuses the same handlers, safety pipeline,
`@requires_plan` gating, and audit JSONL the in-process agent
loop and the `yadirect-agent` CLI use. It is a publishing
adapter, not a parallel implementation.

[cli]: https://github.com/wong2/mcp-cli

## Install

```bash
git clone git@github.com:Kozharina/yadirect-agent.git
cd yadirect-agent
make install         # creates .venv, installs the package
```

You should now have `yadirect-agent` on your PATH. Verify:

```bash
yadirect-agent --version
```

## Configure

Create `agent_policy.yml` in the project root or wherever you
point `AGENT_POLICY_PATH`. A minimal policy is enough to start —
the env-backstop on the budget cap (M2.4) is always in force:

```yaml
account_daily_budget_cap_rub: 5000
rollout_stage: shadow
```

Set the required env vars (in your shell profile, a `.envrc`, or
the MCP `env` block below):

```bash
export YANDEX_DIRECT_TOKEN='...'
export YANDEX_METRIKA_TOKEN='...'
export ANTHROPIC_API_KEY='...'      # required by the agent loop; MCP server itself doesn't need it
export YANDEX_USE_SANDBOX=true       # ALWAYS start in sandbox
export AGENT_MAX_DAILY_BUDGET_RUB=5000
export AGENT_POLICY_PATH=/path/to/agent_policy.yml
```

## Claude Desktop

Add the following to Claude Desktop's MCP configuration. On
macOS the file is at
`~/Library/Application Support/Claude/claude_desktop_config.json`.
On Windows: `%APPDATA%\Claude\claude_desktop_config.json`.

### Read-only mode (default — recommended for first run)

```json
{
  "mcpServers": {
    "yadirect-agent": {
      "command": "yadirect-agent",
      "args": ["mcp", "serve"],
      "env": {
        "YANDEX_DIRECT_TOKEN": "your-token-here",
        "YANDEX_METRIKA_TOKEN": "your-token-here",
        "YANDEX_USE_SANDBOX": "true",
        "AGENT_POLICY_PATH": "/absolute/path/to/agent_policy.yml",
        "AGENT_MAX_DAILY_BUDGET_RUB": "5000",
        "AUDIT_LOG_PATH": "/absolute/path/to/logs/audit.jsonl"
      }
    }
  }
}
```

In this mode the agent in Claude Desktop sees three tools:
`list_campaigns`, `get_keywords`, `validate_phrases`. **It cannot
call any mutating tool** — they're not registered.

Restart Claude Desktop after editing the config. The new tools
appear under the slider icon in the chat input.

### Write mode (after success-gate review)

Once the read-only flow is working and you've reviewed `audit.jsonl`,
opt in to mutations by adding `--allow-write` (or
`MCP_ALLOW_WRITE=true`):

```json
{
  "mcpServers": {
    "yadirect-agent": {
      "command": "yadirect-agent",
      "args": ["mcp", "serve", "--allow-write"],
      "env": { "...": "..." }
    }
  }
}
```

Now the agent can also see `pause_campaigns`, `resume_campaigns`,
`set_campaign_budget`. **Every mutating call still flows through
`@requires_plan`** — `set_campaign_budget` and `resume_campaigns`
return `{status: "pending", plan_id: ...}` and you must run
`yadirect-agent apply-plan <id>` from a terminal to actually
apply. `pause_campaigns` is auto-approved by default
(`auto_approve_pause=True` in the policy) and completes in one
shot without an apply-plan step — it's reversible (just resume)
and the audit JSONL records every pause regardless. The MCP
path cannot bypass any of these.

**`set_keyword_bids` is NOT exposed over MCP yet**, even with
`--allow-write`. `BiddingService.apply` doesn't have its
`@requires_plan` gate yet (KS#2 / KS#4 wiring is the next safety
PR), and the MCP layer keeps it on a denylist until then —
otherwise an MCP client could set arbitrary bids without a
safety review. Run keyword bid changes through the in-process
agent loop (`yadirect-agent run "..."`) until the gate lands.

**Flag/env precedence**: `--allow-write` and `MCP_ALLOW_WRITE` are
both treated as enabling. The CLI flag wins on its own; the env
can also enable. The env CANNOT disable a flag-set true. If you
remove `--allow-write` from `args` to roll back to read-only,
also clear `MCP_ALLOW_WRITE` from the `env` block — otherwise
write mode silently stays on.

## Operator workflow with mutations

Suppose the agent says "I'll lower the budget on campaign 42 to 800
RUB":

1. Agent calls the `set_campaign_budget` tool.
2. MCP server routes through `CampaignService.set_daily_budget` →
   `@requires_plan` → pipeline → `confirm` → plan persisted.
3. The tool returns
   `{"status": "pending", "plan_id": "abc123def456",
   "preview": "set daily budget on campaign 42 to 800 RUB",
   "next_step": "Run \`yadirect-agent apply-plan abc123def456\` to confirm."}`.
4. Claude Desktop relays the structured response — including the
   exact next-step command — to you.
5. You open a terminal:
   ```bash
   yadirect-agent plans show abc123def456    # inspect
   yadirect-agent apply-plan abc123def456    # actually apply
   ```
6. The mutation hits Direct (sandbox by default). Both the
   `apply_plan.requested|.ok` and `set_campaign_budget.requested|.ok`
   events land in `audit.jsonl`.

## Logs

- **MCP protocol** uses stdout. Don't try to parse it.
- **Operator-facing structured logs** go to stderr. Pipe stderr
  through `jq` if you want pretty JSON:
  ```bash
  yadirect-agent mcp serve --allow-write 2> >(jq -c)
  ```
- **Audit JSONL** (the durable record of every action by agent
  and operator) lives at `AUDIT_LOG_PATH`. Default
  `./logs/audit.jsonl`. Privacy redaction strips KS#3 / KS#7
  PII keys at the sink boundary.

## Sandbox vs production

`YANDEX_USE_SANDBOX=true` is the default and **must stay that way**
until you have at least one full day of clean audit logs in
write-mode. Flipping to production is a one-line env change but it
also flips real money on. The recommended rollout sequence:

1. **shadow stage** (read-only MCP): one week. Inspect what the
   agent reads, how it phrases changes.
2. **assist stage** (`rollout promote --to assist`): allow pause +
   negative keywords. Two weeks. Compare its proposals to your own
   judgement.
3. **autonomy_light** then **autonomy_full**: only after the
   `apply-plan` rejection rate drops below a threshold you trust.

Each `rollout promote` writes to the same audit JSONL and is
recoverable via `rollout status`.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `policy_file_not_found` warning at startup | `AGENT_POLICY_PATH` points at a non-existent file. Default Policy is used: read-only (`shadow`). Mutations rejected. |
| Every `set_campaign_budget` returns `status: rejected` | `rollout_stage: shadow` (the safe default). Run `rollout promote --to autonomy_full` once you're ready. |
| Tool not visible in Claude Desktop | Either `--allow-write` wasn't passed (write tools hidden by default) or you didn't restart Claude Desktop after editing the config. |
| `apply-plan` exits 2 with "rejected by re-review" | The snapshot drifted between plan creation and apply. Re-propose the change; the policy now sees current state. |
| Plan appears in `plans list` but `apply-plan` says "not found" | The state-file path drifted (e.g. `AUDIT_LOG_PATH` changed between the agent run and the apply-plan invocation). Plans live next to the audit log; keep paths consistent across launches. |

See also: [`docs/ARCHITECTURE.md`](./ARCHITECTURE.md),
[`docs/TECHNICAL_SPEC.md`](./TECHNICAL_SPEC.md) §M3.
