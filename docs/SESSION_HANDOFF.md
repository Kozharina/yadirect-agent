# Session handoff — 2026-04-27

> **Read first** when opening a new Claude Code session in this repo.
> Then `docs/BACKLOG.md` for full active queue, `CLAUDE.md` for
> operational rules. Total bootstrap should stay under 3 min.

## Current state (one-liner)

Safety layer (§M2) + MCP server (§M3) **fully shipped**. Every
mutating service method gated through `@requires_plan` across all
entry points (CLI / agent loop / MCP). 517 tests, mypy strict,
ruff clean. **PR #33 (BiddingService gating) awaiting CI + merge.**

## Where the hands are

- **Branch**: `feat/m2-bidding-service-gating` (commit `67f08ce`).
- **PR open**: <https://github.com/Kozharina/yadirect-agent/pull/33>
- **Status**: security-auditor returned `request changes` →
  closed all (1 CRITICAL audit-sink contract + 2 HIGH +
  3 MEDIUM + 1 LOW). Awaiting CI green, then squash-merge.
- **Working tree should be clean** when next session opens (after
  the merge). If not — first action is `git status`.

## What just happened (last 6 PRs)

| # | Title | Status |
|---|---|---|
| #28 | M2.3b audit sink wiring | ✅ merged |
| #29 | M2.4 env-backstop | ✅ merged |
| #30 | M2.5 staged rollout | ✅ merged |
| #31 | pause/resume gating | ✅ merged |
| #32 | M3 MCP server | ✅ merged |
| #33 | BiddingService gating | 🟡 awaiting merge |

## Next 3 candidate tasks (priority order)

User has not committed to one — confirm before starting.

### 1. **Tech-debt sweep** (1–2 PRs, 1–2 days)

Three recent auditor follow-ups that are short and unblock
honest claims about safety:

- **KS#2/KS#4 `skipped` semantics on empty bid snapshot**
  (auditor M2-bidding H-1). Currently both checks return `ok`
  with empty snapshot; should return `skipped` so audit
  signal is honest. ~50 LOC + tests.
- **Per-keyword `AccountBidSnapshot` reader**. Extends
  `models/keywords.Keyword` with bid + QS fields, extends
  `DirectService.get_keywords` to fetch them, populates
  `_build_bid_context`. After this: KS#2 max-CPC + KS#4 QS
  guard actually enforce. ~150 LOC + tests.
- **`_infer_actor` frame-walk dedup**. Extract into
  `audit.infer_actor_from_frame()`. Trivial.

### 2. **M4 — real Wordstat** (multi-PR, several days)

§M4 of `docs/TECHNICAL_SPEC.md`. Provider protocol, Wordstat API
implementation (gated on real access), KeyCollector CSV bridge,
embeddings-based clustering, negative-keyword cleaner. Reference:
`docs/PRIOR_ART.md` → "Agentic PPC Campaign Management".

Suggested split:
- M4.1: `WordstatProvider` Protocol + `KeyCollectorCsvProvider`
  (no API access needed; works against operator's CSV export).
- M4.2: real Wordstat API impl behind a feature flag.
- M4.3: embeddings clustering (`services/semantics.py` has
  `_cluster_key` already; extend with embedding-based grouping).
- M4.4: negative-keyword cleaner — surfaces candidates;
  agent proposes via `add_negative_keywords`.

### 3. **M7.2 — agent evals** (1–2 PRs, 1 day)

Today `make check` is green = code OK. There's no proxy for
**agent quality**. Add 10–20 typed task evals under
`tests/evals/`:
- "pause all campaigns with CTR < 0.5%"
- "raise bids on top 5 converting keywords by 20%"
- per-eval metrics: iterations, tokens, correctness flag.
- `make evals` target; per-PR run optional (cost-controlled).

Without evals, every M4/M5 PR is "looks right, checked manually".
With evals, regressions in agent reasoning surface as red.

## Critical context for new session

### Conventions, not negotiable
- **Sandbox-by-default**: `YANDEX_USE_SANDBOX=true` always; never
  flip without explicit user confirm (`CLAUDE.md#non_negotiables`).
- **TDD**: every behaviour change has a visible `test:` → `feat:`/
  `fix:` commit pair in PR history. Pure `refactor:` / `docs:` /
  `chore:` exempt.
- **One logical change per commit**. Conventional Commits format:
  `<type>(<scope>): <imperative subject>`.
- **Every session ends green**: `make check` passes before claiming
  done. mypy strict + ruff format + ruff check + 517 tests.
- **Security-auditor pass before each M2 PR merge** (any PR
  touching safety code). Often returns ≥1 actionable finding;
  expect to fix and re-audit. CLAUDE.md mandates this.

### Architecture invariants
- **CampaignService / BiddingService** wrap mutating methods with
  `@requires_plan(action, resource_type, preview_builder,
  context_builder, resource_ids_from_args)`. All inherit the
  same pattern: `_resolve_safety()` raises if pipeline / store /
  audit_sink missing; `_infer_actor()` walks frames for
  `_applying_plan_id`; `_do_<method>` extracted so `audit_action`
  wraps a single body.
- **`build_default_registry(settings)`** is the canonical
  constructor for the tools registry. It calls
  `build_safety_pair(settings)` once → `(SafetyPipeline,
  PendingPlansStore, JsonlSink)` triple shared by every gated
  factory. SessionState (cross-tool TOCTOU register) survives
  one agent run.
- **`@requires_plan` decorator** lives in
  `src/yadirect_agent/agent/executor.py`. Behaviour:
  - `confirm` → plan persisted to JSONL store, raises
    `PlanRequired`.
  - `reject` → raises `PlanRejected`, plan NOT persisted.
  - `allow` → wraps method, calls
    `pipeline.on_applied(context)` after success.
  - `_applying_plan_id=` kwarg bypasses everything (apply-plan
    re-entry only).
- **`apply_plan(plan_id, *, store, pipeline, service_router,
  audit_sink)`**: re-reviews, dispatches via service_router,
  marks `applied` BEFORE `on_applied` fires (auditor C-1
  ordering: prevents double-spend if `on_applied` raises).
- **MCP server**: `yadirect_agent.mcp.server.build_mcp_server`.
  Read-only by default; `--allow-write` exposes gated mutations.
  `_MCP_WRITE_TOOLS_DENYLIST` is empty (mechanism preserved +
  tested via monkeypatch).
- **Audit sink**: `JsonlSink` writes via `asyncio.to_thread`,
  atomic-on-rename, redacts `_PRIVATE_KEYS = {"new_queries_sample",
  "missing"}` at sink boundary. Emit failures NEVER mask the
  wrapped operation's outcome (auditor M2.3a CRITICAL fix).

### Operational tooling
- **`yadirect-agent run "<task>"`** — one-shot agent.
- **`yadirect-agent plans list / show / ...`** — pending plans.
- **`yadirect-agent apply-plan <id>`** — operator approval.
- **`yadirect-agent rollout status / promote --to <stage>`** —
  rollout-stage transitions. State-file overrides YAML.
- **`yadirect-agent mcp serve [--allow-write]`** — MCP stdio
  server.
- **`yadirect-agent doctor`** — env diagnostics.

### File layout the new session will touch most
- `src/yadirect_agent/agent/{executor,pipeline,plans,safety,
  tools}.py` — safety core.
- `src/yadirect_agent/services/{campaigns,bidding,semantics}.py`
  — service layer (where gating is wired).
- `src/yadirect_agent/{audit,rollout}.py` — supporting modules.
- `src/yadirect_agent/mcp/server.py` — MCP adapter.
- `src/yadirect_agent/cli/main.py` — typer subapps + service
  router.
- `tests/unit/{agent,services,cli,mcp}/` — one test file per
  source module.
- `docs/BACKLOG.md` — single source of truth for queue + tech
  debt + done.
- `docs/CLAUDE.md` — operational protocol.
- `docs/TECHNICAL_SPEC.md` — milestone reference.

### Recent gotchas worth remembering
- Rich + typer wrap `--help` text with ANSI escapes that break
  substring assertions in CI. Read flag docs via
  `typing.get_type_hints(include_extras=True)`, not via
  `runner.invoke(--help).output`.
- `BidUpdate` is now pydantic, not a dataclass — frozen
  dataclasses crash `model_dump_json` for plan persistence.
- `model_copy(update=...)` does NOT re-validate by default;
  this bit us on M2.4 negative-cap. `Field(ge=...)` on the
  Settings field is the right enforcement layer.
- mypy version-skew between in-venv and pre-commit mirror:
  same code surfaces different error codes. Confine MCP-SDK
  decorator gaps to one file with broad `# type: ignore`.
- CodeQL flags `...` Protocol bodies as `py/ineffectual-statement`
  and `pytest.raises` blocks as `py/unreachable-statement` —
  both are false positives; dismiss in the Security tab with
  the standard reasoning.

### User preferences
- **Russian for chat**, **English for code/commits/docs**
  (memory/user_language.md).
- **Trusts engineering judgement** — when stuck on a small
  decision, decide and proceed; ask only on substantive
  forks. Recent track record: user said "сделай как считаешь
  лучшим выбором" multiple times.
- **Push / merge only on explicit go**. Auditor passes happen
  automatically before merge.
- **Authorisation for external services** (`gh auth login`,
  publishing) is interactive on user side; we wait until
  user reports done.

## What to do first when this session opens

1. **`git checkout main && git pull`** — sync (assumes PR #33
   has merged).
2. **`git branch -d feat/m2-bidding-service-gating`** —
   cleanup.
3. **`make check`** — verify green tree.
4. **Read `docs/BACKLOG.md`** Active queue + Tech debt.
5. **Confirm with user** which of the three candidate tasks
   above is next. Do NOT start without confirmation.

If PR #33 hasn't merged yet:
- Check CI: `gh pr checks 33`.
- If failing: investigate → fix → push → wait.
- If passing but not merged: ask user before merging.
