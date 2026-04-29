# Operating yadirect-agent

This file is the operator handbook — both the **product-level user
journey** (how a real account owner lives with the agent over weeks
and months) and the **operator-level worked example** (concrete CLI
output and apply-plan flow). Read the journey first; it's the anchor
every feature in [`docs/TECHNICAL_SPEC.md`](./TECHNICAL_SPEC.md) is
ultimately scored against.

The agent has two transports — **CLI** (cron, scripted, terminal) and
**MCP server** (Claude Desktop, Claude Code, any MCP-compatible client).
Both share the same safety pipeline, `@requires_plan` gating, and audit
JSONL — different shells, identical guarantees. For most users the
Claude Desktop path is the primary one; CLI is for power users and
servers without a chat client.

## User journey: how an account owner lives with this product

The persona we design for: **Anna**, owner of a children's clothing
e-shop. Runs PPC herself, no media buyer on staff, ~50K RUB/month
budget on Yandex.Direct. She's not a developer. Her pain is the
30–60 minutes a day spent staring at the cabinet and tweaking minus
words, when she'd rather be sourcing new stock.

This is the path she takes — not aspirational, **the contract** the
product owes her at every step.

### Phase 0 — Discovery (≤ 10 min from install to first value)

Anna installs the package once (one command — `pip install`, `brew`,
or `docker run`) and runs a single helper that wires the agent into
her Claude Desktop config. From that point on, **she never opens the
terminal again** unless she wants to.

She types into Claude Desktop, in plain Russian: *"помоги настроить
агента для Яндекс.Директа"*. The agent, exposed as an MCP server,
walks her through OAuth in the browser (one click on Yandex's standard
"Allow" page for Direct, another for Metrika) and asks five questions
about her business — niche, ICP, monthly budget, target CPA, what's
forbidden to claim in ads.

Within ten minutes the agent has read her cabinet end-to-end and
written back, in chat: *"three campaigns burned 8K RUB in two weeks
with zero conversions; twelve keywords are in 'rejected' moderation
status which you probably haven't seen; average CPA is 850 RUB while
one of your campaigns is at 320 RUB. Want me to dig into any of these?"*

This first read **does not require an Anthropic API key** — it's a
deterministic rule-based account-health check. Anna sees concrete
value before she's asked to pay anyone anything. If she likes it,
she upgrades to the LLM mode (which unlocks creative generation,
conversational ad-hoc tasks, and human-readable explanations) by
pasting one key.

### Phase 1 — Shadow week (days 1–7)

The agent runs once a day on a schedule it set up itself (LaunchAgent
on macOS, systemd on Linux, Task Scheduler on Windows — Anna doesn't
need to know what cron is).

Each morning she gets one short message in Telegram (or email, her
pick): *"today I would have done these three things: 1) paused
campaign X — 2400 RUB spent in 7 days, 0 conversions; 2) added these
minus words to ad group Y; 3) raised the bid on keyword Z by 15% —
cheapest CPA in the account. I did nothing — I'm in observation mode."*

Each suggestion comes with a one-line **rationale**, not just an
action. Anna reads at breakfast, mentally compares to what she'd do.
After 7 days the agent runs a calibration: *"we agreed on 8 of 10
calls. Ready to take over the safe stuff?"*. She taps yes.

### Phase 2 — Assist (weeks 2–4)

The agent now does, **on its own without asking**, the operations
that are reversible or that *reduce* spend:

- pause underperforming campaigns (always reversible — just resume)
- add minus keywords (always lowers spend)
- small bid corrections within a ±10% daily band

Anything that **spends new money or is hard to undo** — increasing a
budget, resuming a paused campaign, raising a bid above the band,
publishing a new creative, switching strategy — flows as an **approval
request** to Anna's preferred channel:

> *"I want to raise the daily budget on campaign 'autumn collection'
> from 800 RUB to 1100 RUB. Reason: CPA is 290 RUB at a 600 RUB target,
> the campaign is hitting its cap by 14:00 every day — we're losing
> half the day. Approve? ✅ Apply / ❌ Reject / 🤔 Why"*

Tap "Apply" — the change is in within 30 seconds, with a confirmation.
Tap "Why" — the agent expands the reasoning: which 28 days of data,
which alternatives it considered, why +37% and not +20% or +50%.

A weekly digest lands in email every Sunday: spent, conversions, what
moved, what's worrying, what the agent did and why, a forecast for
the coming week. Three readable paragraphs, not a dashboard.

### Phase 3 — Autonomy (week 5+)

After two to three weeks of clean audit (no rollbacks, no rejected
approvals, no kill-switch firings, all metrics in their corridors),
the agent itself proposes promotion to autonomy. Anna accepts.

Now she gets:

- **Mornings**: nothing. Silence is the success state.
- **Weekly**: a 3–5 line digest in Telegram. *"Steady week, +12%
  conversions, CPA holding. Highlight: launched three new creatives
  on the 'strollers' campaign, two are showing early lift —
  re-evaluating in 7 days."*
- **Monthly**: long-form report by email with a forecast and a
  proposed plan for next month.
- **Anomalies only**: a flagged "ATTENTION" message — *"campaign X
  CPA jumped 45% in 48 hours. I paused it to stop the bleed. Need
  your call: if this is your offline promo running, tap 'normal —
  resume'; if not, tap 'investigate'."* The agent **never** does
  large irreversible moves under anomaly conditions; it stops and
  asks.

### Cross-cutting — at any phase, Anna can

- Open Claude Desktop and ask anything ad-hoc:
  *"я завтра в отпуск на неделю, поставь все кампании на паузу с
  понедельника по воскресенье"* → done.
- Roll back any agent run with one command:
  *"отмени всё, что ты сделал вчера"* → exact restore to yesterday's
  morning state.
- Demand an explanation for any past decision:
  *"почему ты вчера снизил ставку на ключе 'детская куртка зима'?"*
  → the agent retrieves the per-decision rationale recorded at the
  time, not a fresh confabulation.
- Step back at any time:
  *"return to the mode where you ask me about everything"* → done,
  immediately, no negotiation.

### The contract these phases imply

1. **Time to first value ≤ 10 minutes**, no payment to anyone, no
   YAML, no terminal beyond one install command.
2. **No surprise mutations.** Anything that spends or is irreversible
   asks first, and asks well — with a reason, not a tool call dump.
3. **One-click rollback.** Every agent action is recorded with enough
   context to undo. There is no "I lost yesterday's state" failure
   mode.
4. **Silence is success.** A working autonomous agent does not write
   to the operator daily. It writes weekly digests, monthly reports,
   and anomaly alerts. That's it.
5. **Stop, don't escape.** Under anomaly the agent halts and asks.
   It never tries to "solve its way out" of a situation it doesn't
   understand.

**This is the test we apply to every feature proposal**: which phase
does it serve? What in this contract becomes impossible without it?
If neither has a clean answer, it doesn't go in the product.

## Worked example: CLI from zero to applied change

End-to-end flow against the sandbox. Assume `make install`, `.env`,
and `agent_policy.yml` are already in place (see `README.md` Quickstart).

```bash
# 1. One-shot read — no plan, no audit gate, just answers.
$ yadirect-agent run "list active campaigns and their daily budgets"
→ tools: list_campaigns
   ┌────┬──────────────────┬───────────┬─────────┐
   │ id │ name             │ state     │ budget  │
   ├────┼──────────────────┼───────────┼─────────┤
   │ 42 │ brand            │ ON        │ 1500.00 │
   │ 51 │ non-brand-search │ SUSPENDED │  800.00 │
   └────┴──────────────────┴───────────┴─────────┘

# 2. Mutating ask — agent forms a plan, the policy gate decides.
$ yadirect-agent run "lower campaign 42 budget to 800 RUB"
→ tools: list_campaigns, set_campaign_budget
   status: pending
   plan_id: abc123def456
   preview: set daily budget on campaign 42 to 800 RUB
   reason:  -47% change exceeds max_daily_budget_change_pct=0.20
   next_step: yadirect-agent apply-plan abc123def456

# 3. Inspect before approving — what exactly is queued?
$ yadirect-agent plans show abc123def456
   created_at: 2026-04-27T18:14:02Z
   action:     campaigns.set_daily_budget
   args:       {"campaign_id": 42, "amount_rub": 800}
   policy_decision: NeedsConfirmation
   reasons:    [budget_change_pct_exceeded]
   baseline:   {"campaign_42_budget_rub": 1500, "ts": "..."}

# 4. Approve. The plan is re-reviewed against fresh state — if the
#    snapshot drifted (>max_snapshot_age_seconds, default 300s), it's
#    rejected and you re-propose.
$ yadirect-agent apply-plan abc123def456
   re-review: ok
   executing: campaigns.set_daily_budget
   ok: campaign_id=42 amount_rub=800

# 5. Audit trail of everything that happened — agent and operator.
$ tail -n 5 logs/audit.jsonl | jq -c '{ts, actor, event}'
   {"ts":"...","actor":"agent","event":"set_daily_budget.requested"}
   {"ts":"...","actor":"agent","event":"plan.persisted"}
   {"ts":"...","actor":"operator","event":"apply_plan.requested"}
   {"ts":"...","actor":"operator","event":"set_daily_budget.ok"}
   {"ts":"...","actor":"operator","event":"apply_plan.ok"}
```

What you've just seen is the full safety contract: the agent never
mutates without producing a plan, the plan is policy-checked before
*and* re-checked at apply time, and every step is in an append-only log
keyed by `trace_id`.

`pause_campaigns` is the one mutation that auto-applies in a single
shot — it's reversible (just resume) and `auto_approve_pause=True` by
default in the policy. Every other mutation flows through the plan loop
above.

## Claude Desktop / Claude Code integration via MCP

`yadirect-agent` also exposes its tools over the [Model Context
Protocol](https://modelcontextprotocol.io) so any MCP-compatible
client — Claude Desktop, Claude Code, [mcp-cli][cli], custom
agents — can drive the account.

The MCP server reuses the same handlers, safety pipeline,
`@requires_plan` gating, and audit JSONL the in-process agent
loop and the CLI use. It is a publishing adapter, not a parallel
implementation.

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
the MCP `env` block below). Two paths are supported:

**Recommended — interactive OAuth (M15.3):**

```bash
yadirect-agent auth login
```

Opens your default browser to Yandex's consent page, runs a
one-shot HTTP server on `localhost:8765` to catch the redirect,
exchanges the code for an access token, and stores it in your OS
keychain (Keychain on macOS, Credential Manager on Windows,
Secret Service / KWallet / GNOME Keyring on Linux). After this,
`Settings` reads the token automatically — no
`YANDEX_DIRECT_TOKEN` / `YANDEX_METRIKA_TOKEN` needed in
environment.

```bash
yadirect-agent auth status     # check token (masked) and expiry
yadirect-agent auth logout     # clear keychain entry
```

Exit codes for cron / wrappers: `auth login` exits 0 on success,
2 on user-denied / callback-timeout / invalid-grant; `auth status`
exits 0 when logged in and 1 when not (alert on this from cron);
`auth logout` always exits 0 (idempotent). Note: `logout` clears the
LOCAL keychain slot only — Yandex OAuth has no public revocation
endpoint, so the refresh token remains valid server-side until you
manually revoke it at <https://yandex.ru/profile/access>.

**Alternative — env vars** (CI / Docker / headless contexts):

```bash
export YANDEX_DIRECT_TOKEN='...'
export YANDEX_METRIKA_TOKEN='...'
```

Env values win over the keychain when both are present, so a CI
override never collides with a stale local-machine token. The
keychain hydration is fail-soft — a missing or corrupt entry
keeps `Settings` booting without crashing.

**Common to both paths:**

```bash
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

In this mode the agent in Claude Desktop sees five tools:
`list_campaigns`, `get_keywords`, `validate_phrases`,
`explain_decision`, and `account_health`. **It cannot call any
mutating tool** — they're not registered.

`explain_decision(decision_id)` is the read-back tool for recorded
rationales (M20). In chat: *"Почему ты вчера снизил budget на
campaign 42?"* — Claude pulls the `decision_id` from the previous
turn or from `yadirect-agent rationale list`, calls
`explain_decision`, and reports the recorded reason verbatim.
The agent never fabricates a reason — when the `decision_id` is
unknown the tool returns `{status: "not_found"}` and Claude says
so honestly.

`account_health(days=7, goal_id=None)` is the chat mirror of
`yadirect-agent health` (M15.5). Deterministic, rule-based — no
LLM involved on the rules side. In chat: *"проверь моё здоровье"*
/ *"какие сейчас проблемы в кабинете?"* — Claude calls
`account_health()`, receives a structured list of findings
(burning campaigns, high-CPA, etc.) with severity / impact / RUB
estimates, and renders them as a human-readable summary. When
`YANDEX_METRIKA_COUNTER_ID` is not set, the tool returns
`{status: "unconfigured"}` and Claude tells the user which env
var to set rather than failing opaquely.

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
`set_campaign_budget`, and `set_keyword_bids`. **Every mutating
call still flows through `@requires_plan`** — `set_campaign_budget`,
`resume_campaigns`, and `set_keyword_bids` return
`{status: "pending", plan_id: ...}` and you must run
`yadirect-agent apply-plan <id>` from a terminal to actually
apply. `pause_campaigns` is auto-approved by default
(`auto_approve_pause=True` in the policy) and completes in one
shot without an apply-plan step — it's reversible (just resume)
and the audit JSONL records every pause regardless. The MCP path
cannot bypass any of these.

Every mutating tool requires a `reason` field (M20 slice 2) — the
LLM articulates WHY before the safety pipeline runs. The reason
is recorded as `summary` on the `Rationale` row written to
`rationale.jsonl`, which `explain_decision` later returns
verbatim when you ask "why did you do X?".

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
