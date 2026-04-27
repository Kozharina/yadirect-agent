# yadirect-agent

> An autonomous AI agent for Yandex.Direct that does the daily PPC chores —
> with a safety layer that refuses to spend money it shouldn't.

[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![CI](https://github.com/Kozharina/yadirect-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/Kozharina/yadirect-agent/actions/workflows/ci.yml)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![Checked with mypy](https://www.mypy-lang.org/static/mypy_badge.svg)](https://mypy-lang.org/)

## What it does

You give it a task in plain language —
*"pause everything with CTR below 0.5% over the last 7 days"*,
*"raise bids on the top-5 converting keywords by 20%"* — and it talks to
Yandex.Direct and Yandex.Metrika, plans the change, runs it through a
safety pipeline, executes, and appends an audit record.

Two interchangeable transports over the same core:

- **CLI** — `yadirect-agent run "..."` for cron or ad-hoc work.
- **MCP server** — `yadirect-agent mcp serve` for Claude Desktop and
  Claude Code. Same tools, same safety, different shell.

## The problem it solves

Day-to-day PPC management is dozens of small, repetitive decisions on the
same handful of signals (CTR, CPA, Quality Score, query drift). They eat
attention and they're easy to drop. Handing them to "just an LLM with API
keys" is how you wake up to a drained budget.

This agent does the chores **and** treats spending as the dangerous
operation it is: every mutating call is gated, every change is auditable,
defaults refuse to do anything irreversible without a human in the loop.

## Status

Pre-alpha. The shipped scope, the queue, and the discovered tech debt all
live in **[`docs/BACKLOG.md`](./docs/BACKLOG.md)** — that's the single
source of truth, README will not try to mirror it. Roadmap by milestone:
**[`docs/TECHNICAL_SPEC.md`](./docs/TECHNICAL_SPEC.md)**.

At a coarse level: reads, safe pauses, gated mutations (resume / budget /
bids via `apply-plan`), full safety pipeline (7 kill-switches +
plan→confirm→execute + audit + staged rollout), CLI and MCP work today.
Real Wordstat, A/B testing, Metrika reporting and alerts don't yet.

## Quickstart

Requires Python 3.11+. We use [`uv`](https://github.com/astral-sh/uv).

```bash
git clone git@github.com:Kozharina/yadirect-agent.git
cd yadirect-agent
make install                                    # venv + dev deps
cp .env.example .env                            # fill tokens; keep YANDEX_USE_SANDBOX=true
cp agent_policy.example.yml agent_policy.yml    # set account_daily_budget_cap_rub
make check                                      # lint + type + tests must be green

yadirect-agent run "list all campaigns in sandbox"
```

Driving it from Claude Desktop / Claude Code:
**[`docs/OPERATING.md`](./docs/OPERATING.md)**.

## Safety in one screen

Four independent layers. Any one of them can refuse the operation.

1. **Sandbox by default.** `YANDEX_USE_SANDBOX=true` hits
   `api-sandbox.direct.yandex.com` and cannot move real money. Flipping
   it is a deliberate human action, not something the agent can talk you
   into.
2. **Plan → confirm → execute.** Every mutating call is serialised into
   an `OperationPlan`, run through the policy pipeline (budget cap,
   max-CPC, required negatives, QS guard, budget-balance shift,
   conversion integrity, query drift), and only then dispatched. The
   policy lives in `agent_policy.yml` and cannot be overridden from the
   model's context.
3. **Audit JSONL.** Every action — agent and operator — is appended to
   `logs/audit.jsonl` with a `trace_id`, request/response shapes, and a
   reversibility marker. Append-only. Secrets are stripped at the sink.
4. **Staged rollout.** `shadow → assist → autonomy_light → autonomy_full`
   in `agent_policy.yml`. Promotion is an explicit
   `yadirect-agent rollout promote` call that itself gets audited.

The deeper version: [`docs/TECHNICAL_SPEC.md` §M2](./docs/TECHNICAL_SPEC.md).
Reporting a vulnerability: [`SECURITY.md`](./SECURITY.md).

## Commands

```bash
yadirect-agent run "<task>"               # one-shot agent run
yadirect-agent chat                       # interactive REPL
yadirect-agent list-campaigns [--state]   # direct call, no model (debug)
yadirect-agent plans list | show <id>     # inspect pending plans
yadirect-agent apply-plan <id>            # operator approval — actually executes
yadirect-agent rollout status | promote   # autonomy stage transitions
yadirect-agent mcp serve [--allow-write]  # MCP stdio server
yadirect-agent doctor                     # environment diagnostics
```

## Where to look next

| You want to…                                  | Read                                                |
| --------------------------------------------- | --------------------------------------------------- |
| Use it from Claude Desktop / Claude Code      | [`docs/OPERATING.md`](./docs/OPERATING.md) |
| Understand the layers and what depends on what| [`docs/ARCHITECTURE.md`](./docs/ARCHITECTURE.md)    |
| See what's queued, blocked, in tech debt, done| [`docs/BACKLOG.md`](./docs/BACKLOG.md)              |
| Read the milestone roadmap                    | [`docs/TECHNICAL_SPEC.md`](./docs/TECHNICAL_SPEC.md)|
| Contribute code                               | [`docs/CODING_RULES.md`](./docs/CODING_RULES.md), [`docs/TESTING.md`](./docs/TESTING.md), [`docs/REVIEW.md`](./docs/REVIEW.md) |
| Know how Claude Code itself behaves in this repo | [`CLAUDE.md`](./CLAUDE.md)                       |
| Report a vulnerability                        | [`SECURITY.md`](./SECURITY.md)                      |

## License

MIT — see [`LICENSE`](./LICENSE).
