# Backlog

> Single source of truth for what's queued, in progress, blocked, done,
> and idea-level. Read at the start of every session
> (`CLAUDE.md#bootstrapping_a_fresh_session`). Updated at the end of
> every PR (`CLAUDE.md#workflow_per_task`).
>
> **Format**: plain checkboxes, no issue numbers required. Ordering
> inside each section is priority — top is next. Each active item links
> to the relevant milestone section in
> [`docs/TECHNICAL_SPEC.md`](./TECHNICAL_SPEC.md) where applicable, so
> the "why" is always one click away.
>
> **Conventions**:
> - `chore(backlog): …` / `docs(backlog): …` is a valid commit scope.
> - Moving an item across sections counts as an update — do it in the
>   same PR as the change that caused the move, not later.
> - This file is version-controlled; `git log -p docs/BACKLOG.md`
>   answers "when did we decide X was blocked / dropped / done?".

## Active queue

Ordered — top is what I take next.

### 🔥 Before M2 (safety)

- [ ] **M7.1 dotyazhka**: `tests/unit/services/test_bidding.py` +
      `tests/unit/services/test_semantics.py`. First proper TDD PR:
      tests per behaviour, red commit → green commit pairs visible.
      Raises coverage gate 78 → 80 in the same PR.
- [ ] **PR-C: `yadirect-agent doctor` command** — env + Anthropic +
      Direct sandbox + policy-file diagnostics. TDD. See
      [§M1.4](./TECHNICAL_SPEC.md) context for CLI conventions.

### 🛡️ M2 — safety layer (one PR per kill-switch)

Each one is TDD, with `security-auditor` sub-agent review before merge.
All seven reference
[`docs/PRIOR_ART.md`](./PRIOR_ART.md) → "Agentic PPC Campaign Management".

- [ ] **Kill-switch #1 — Budget caps** (§M2.0 rule 1, most important:
      spent budget is irreversible). Account-level and
      campaign-group-level hard ceilings.
- [ ] **Kill-switch #2 — Max CPC per campaign** (§M2.0 rule 2).
- [ ] **Kill-switch #3 — Negative-keyword floor** (§M2.0 rule 3):
      required minimum list (e.g. free/скачать/отзывы/вакансии).
- [ ] **Kill-switch #4 — Quality Score guardrail + protected metric**
      (§M2.0 rule 4, §M2.6). QS as constraint, never objective;
      monitor-only, alert on > 1-point drop over 7 days.
- [ ] **Kill-switch #5 — Budget-balance drift** (§M2.0 rule 5):
      X% cap on cross-campaign share shift per day.
- [ ] **Kill-switch #6 — Conversion integrity** (§M2.0 rule 6):
      daily Metrika goal-count sanity check; blocks writes on anomaly.
- [ ] **Kill-switch #7 — Query drift detector** (§M2.0 rule 7):
      weekly new-query-share alert.
- [ ] **M2.1 + M2.2: Policy schema & `plan → confirm → execute`** —
      `agent/safety.py::Policy` (pydantic), `agent_policy.yml` loader,
      `@requires_plan` decorator, `pending_plans.jsonl`,
      `yadirect-agent apply-plan <id>` command.
- [ ] **M2.3: Audit sink** — `audit.py::AuditEvent`, JSONL async writer,
      `*.requested` / `*.ok` / `*.failed` emissions in every mutating
      service method.
- [ ] **M2.4: Daily-budget hard guard** — pre-op check that summed
      active-campaign budgets stay ≤ `AGENT_MAX_DAILY_BUDGET_RUB`.
- [ ] **M2.5: Staged rollout** — `rollout_stage` field in policy,
      `yadirect-agent rollout promote` (audit-logged, requires human
      confirmation).

### 🔌 M3 — MCP server

- [ ] **M3.1 + M3.2: MCP bootstrap + flag gating** (§M3): reuse the
      `ToolRegistry` from M1; `--allow-write` flag (or
      `MCP_ALLOW_WRITE=true`); structured results + human-readable
      strings from each tool. Use skill `mcp-builder` as reference.
- [ ] **M3.3: Claude Desktop docs** — `docs/CLAUDE_DESKTOP.md` with a
      ready-to-paste `mcpServers` JSON block.

### 🔎 Semantics, A/B, reporting (later milestones)

- [ ] **M4 — real Wordstat** (§M4): provider protocol, Wordstat API
      impl (gated by real access), KeyCollector CSV bridge,
      embeddings-based clustering, negative-keyword cleaner, upload to
      ad group respecting Direct's 200-keywords-per-group cap.
- [ ] **M5 — A/B testing service** (§M5): `AbTest` model, Mann-Whitney
      U for CPA/ROAS, bootstrap CIs, conclude auto-pauses losers.
      Reference: `ericosiu/ai-marketing-skills/growth-engine`
      (in PRIOR_ART).
- [ ] **M6 — Reporting & alerts** (§M6): Metrika `get_goals`,
      `get_report`, `conversion_by_source`; `services/reporting.py`;
      `services/alerts.py`; `alerts.jsonl`.

## In progress

*(empty — nothing checked out right now)*

Update this section when a feature branch is pushed; move back out when
the PR merges or is abandoned.

## Blocked / waiting

- [ ] **Codecov integration** — adds a live coverage badge to README.
      Needs user action: register the repo at codecov.io, add
      `CODECOV_TOKEN` to GH Actions secrets, then I wire up the
      `codecov/codecov-action`. Not urgent; CI artefact `coverage.xml`
      is the fallback.

## Tech debt / follow-ups

Accumulated work that isn't blocking but will sting later.

- [ ] `clients/direct.py` methods with no `respx` tests:
      `get_adgroups`, `get_ads`, `add_keywords`, `set_keyword_bids`,
      `fetch_report`. File coverage sits at ~32%. Fold into the PR
      that first uses each method in a service path.
- [ ] `clients/metrika.py` is a stub (0% coverage). Filled out in M6.
- [ ] `logging.py` at ~47% coverage — `configure_logging` has side
      effects that are awkward to unit-test. Options: snapshot with
      `capsys`, or accept the gap and note it.
- [ ] Wire `import-linter` (or a ruff-arch rule) to *enforce* the
      layer boundaries described in `docs/ARCHITECTURE.md` rather than
      relying on review.
- [ ] Anthropic prompt caching in `agent/loop.py` — the system prompt
      is resent every turn and will be worth caching once prompts grow.
      Target: 50–90% savings on repeat turns.
- [ ] Verify the Anthropic model string (`claude-opus-4-7`) against the
      latest available when the first real API call lands.
- [ ] `make test-cov` gate vs. `make test` default — think about
      whether `check` should run `test-cov` instead of plain `test`
      to keep the gate enforced locally, not only in CI.
- [ ] **Raise coverage gate 78 → 80** once M7.1 dotyazhka lands. Gate
      value lives in two places (CI yaml and Makefile) — raise both.
- [ ] **Pre-branch ritual in CLAUDE.md** — bug hit once: creating a
      new branch without first `git switch main && git pull --ff-only`
      led to stale base and a merge conflict. Add an explicit
      checklist to `<workflow_per_task>`: sync main → delete merged
      local branches → `git fetch --prune` → only then `git switch -c`.
- [ ] **Copilot Autofix review policy** — `github-advanced-security`
      can push "potential fix" commits straight to the PR branch when
      the "Apply as commit" button is clicked. The fix can be
      syntactically incomplete (e.g. remove dead function, leave the
      imports). Rule to add to `docs/REVIEW.md` tier 1: every
      autofix commit must be followed by a manual `make check` before
      re-requesting review. Caught after PR for
      `chore/codeql-first-scan-cleanup`.

## Ideas (no commitment)

Things worth considering later; promote to *Active* only when their
turn actually comes.

- [ ] Mutation testing via `mutmut` or `cosmic-ray`, weekly CI cron.
      Proves the test suite catches real mutations, not just lines.
- [ ] `hypothesis` property-based tests for
      `services/semantics.normalize` and `_cluster_key`.
- [ ] **Replay mode**: record a real agent session (JSONL of model
      turns), replay it against the current `FakeAnthropic` fixture as
      a regression test for prompt / tool-schema changes.
- [ ] Prompt versioning (`SYSTEM_PROMPT_V1`, `_V2`, …) + A/B on an
      evals dataset once we have one.
- [ ] `Dockerfile` + GitHub Container Registry workflow so the agent
      can be run as a cron container instead of pip-installed.
- [ ] Auto-generated `CHANGELOG.md` via `release-please` or
      `git-cliff`, tied to conventional commits.
- [ ] `CONTRIBUTING.md` + `CODE_OF_CONDUCT.md` when/if the project
      attracts external contributors.
- [ ] **Cost tracking** — rubles per tool call, tokens per turn,
      surfaced on `AgentRun` and written to audit.
- [ ] Project-local sub-agent `yadirect-safety-auditor` — preloaded
      with `PRIOR_ART` + `TECHNICAL_SPEC §M2` + `ARCHITECTURE`,
      reviewed against every safety-layer PR.
- [ ] Agent **evals** dataset: 10–20 typed tasks ("pause all campaigns
      with CTR < 0.5%", "raise bids on the top 5 converting keywords
      by 20%"), run per-PR, metrics: iterations, tokens, correctness.

## Done

Last 10 items (newest at top). Older items are available via
`git log -p docs/BACKLOG.md`.

- [x] **CodeQL first-scan cleanup** — 2 real `Note` alerts fixed in
      code (`test_campaigns.py` dotted-path monkeypatch; unused `limit`
      params prefixed with `_`). 3 Protocol-stub false positives
      dismissed in the Security tab with reason "false positive —
      `...` is the idiomatic `typing.Protocol` method body".
- [x] **PR-B: security baseline** — `SECURITY.md` (GH private advisory
      workflow), `.github/dependabot.yml` (weekly, grouped, labeled
      `dependencies`/`python` and `github-actions`),
      `.github/workflows/codeql.yml` (push + PR + weekly cron, pack
      `security-and-quality`, Python only).
- [x] **docs(backlog): introduce BACKLOG.md + rules** — merged as #5.
- [x] **PR-A: coverage gate** — `pytest-cov` (`--cov-fail-under=78`),
      `pytest-randomly`, `pytest-timeout=10` with two retry tests
      explicitly marked `timeout=60`. Merged as #4.
- [x] **docs/enforce-tdd** — `TDD as default` in `CLAUDE.md`,
      `<tdd_workflow>` section in `TESTING.md`, reviewer checklist
      item in `REVIEW.md`, PR-template checkbox. Merged as #3.
- [x] **M1 agent skeleton** — `agent/` (tools, loop, prompts, CLI),
      47 new tests, argument-aware repetition detector, parallel
      reads / serial writes. Merged as #2.
- [x] **Pre-commit setup** — hooks installed, missing runtime deps
      (`httpx`, `structlog`, `tenacity`, `anthropic`, `typer`) added
      to the mypy mirror's `additional_dependencies`. Merged as #1.
- [x] **Initial scaffold + M0** — `pyproject.toml`, layered `src/`
      tree (clients / services / models), `README.md`, `LICENSE`
      (MIT), `Makefile`, `.pre-commit-config.yaml`, CI workflow on
      py3.11 + py3.12, issue/PR templates, branch ruleset,
      navigation docs (`CLAUDE.md`, `ARCHITECTURE.md`,
      `CODING_RULES.md`, `TESTING.md`, `REVIEW.md`).
