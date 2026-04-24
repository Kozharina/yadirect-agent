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


### 🛡️ M2 — safety layer (one PR per kill-switch)

Each one is TDD, with `security-auditor` sub-agent review before merge.
All seven reference
[`docs/PRIOR_ART.md`](./PRIOR_ART.md) → "Agentic PPC Campaign Management".

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

- [ ] **Query-drift follow-ups from KS#7 review**:
  - PRIVACY / M2.3: **Hash or truncate `new_queries_sample` in the
    audit sink path.** KS#7's `CheckResult.details` surfaces up to
    10 raw user search queries for operator review — Direct search
    terms can contain names, addresses, medical phrases. M2.3
    audit sink must redact before log persistence.
  - DESIGN: **Population-based vs reach-weighted drift.** KS#7
    counts distinct queries, so 100 low-impression noise queries
    weigh as much as one high-impression anomaly. Consider an
    impression-weighted variant once Metrika integration (M6)
    provides per-query volume.
  - DESIGN: **List-vs-set-size divergence on SearchQueriesSnapshot.**
    No code path reads `len(snapshot.queries)` today; the check
    always goes through `normalised()`. If a future developer adds
    a raw-count read without going through the set, the
    duplicate-padding surface activates. Consider a guard or a
    `__post_init__` note on the dataclass.

- [ ] **Conversion-integrity follow-ups from KS#6 review**
      (architectural items, for M2.2 pipeline wiring):
  - DESIGN: **Global-gatekeeper marker** — KS#6 is the first check
    where a `blocked` result must abort *every* write in the plan
    (no per-campaign scope). M2.2 pipeline must enforce this
    invariant rather than demoting it to a per-op skip. Consider
    typing the check as a distinct role (e.g. `SystemCheck` vs
    `OperationCheck`) so the dispatcher can't confuse them.
  - DESIGN: **`warn` on empty baseline in autonomous mode** — the
    docstring says M2.2 "can" refuse autonomous writes on warn.
    That's too weak: in fully-autonomous runs, `warn` from KS#6
    should be a hard block; only a human-supervised mode may
    override. Pin this when writing the pipeline runner.

- [ ] **Balance-drift follow-ups from security-auditor review**
      (logged during M2 Kill-switch #5; architectural, not a
      current bypass):
  - MEDIUM: **Baseline-provenance contract** — `BudgetBalanceDriftCheck`
    trusts the `baseline` argument as-is. M2.2 pipeline runner must
    be the sole constructor of baseline, sourced from a read-only
    store with a timestamp assertion, and the baseline's age must
    flow into the M2.3 audit sink so stale baselines (e.g. a failed
    cron leaving last week's data) surface loudly.
  - DESIGN: **No upper-ceiling warning on `max_shift_pct_per_day`**
    — `le=1` allows `0.99999` which functionally disables the
    check. A policy-load-time warn when the value exceeds ~0.7
    would catch accidental near-disablement. Deferred to M2.1's
    full Policy schema.

- [ ] **QS-guardrail follow-ups from security-auditor review**
      (logged during M2 Kill-switch #4; no single-call bypass, but
      load-bearing before the pipeline ships):
  - MEDIUM: **Cross-call bid-ratcheting TOCTOU** — KS#4 is stateless
    per `check()` call. An agent can split an increase across N
    calls (each small-delta against the fresh snapshot) and walk a
    low-QS bid upward while every individual call passes. Must land
    before M2.2 pipeline runner: a session-scoped
    `max_approved_bid_per_keyword` register consulted and updated
    inside the pipeline's per-turn execution.
  - LOW: **None-current-bid defers to allow** — if either the
    current or the new bid on a given field is None, KS#4 and KS#2
    skip (cannot prove an increase / cap violation). An adversarial
    snapshot builder that leaves bids as None slips guards. The
    M2.3 audit sink should emit a `warn` for every deferred-None
    case, and M2.2 snapshot builder must read bids eagerly.
  - §M2.6 **QS trending** — median campaign QS drop > 1 point
    over 7 days triggers alert + halt. Needs historical snapshots
    (time-series sink) + background job. Out of scope for single-
    point KS#4; scheduled for after the audit sink (M2.3) provides
    a place to read daily QS writes from.
  - DESIGN: **`KeywordSnapshot` post-init is now enforcing QS
    integrity** — same pattern could migrate to a pydantic model
    for consistency with every policy class in this module. Not
    urgent: the dataclass+__post_init__ is functionally equivalent
    and keeps the import surface narrow.

- [ ] **Negative-keyword-floor follow-ups from security-auditor
      review** (logged during M2 Kill-switch #3; lower severity /
      design-level, not current bypasses):
  - DESIGN: **Phrase-modifier semantics** — Yandex Direct lets
    negatives carry modifiers like `"отзывы +клиентов"` (plus-form
    forcing exact match). KS#3's set-equality treats that as
    distinct from bare `"отзывы"` and blocks the resume (safe
    default), but operators will hit false positives. Document
    in TECHNICAL_SPEC when M2.1 lands the full Policy.
  - DESIGN: **Duplicate/redundant policy entries silently collapsed
    by set construction** — `["бесплатно", "Бесплатно"]` folds to
    one phrase. Matching works; operator gets no feedback that
    their policy contains redundant entries. Add a load-time warn
    when `len(normalised_set) < len(input_list)` in M2.1's policy
    loader.
  - DESIGN: **Multi-campaign violation aggregation** — KS#3 (like
    KS#1/#2) returns on the first violation. Multi-resume plans
    require round-trips for the operator to discover every
    non-compliant campaign. M2.2 pipeline orchestrator should
    consider collecting all violations before presenting a verdict.
  - DESIGN: **ENDED → ON transitions** — Direct may not honour a
    resume on ENDED/ARCHIVED campaigns at the API layer, but our
    projection treats them as spending once `new_state="ON"` is
    applied. Add a campaign-state whitelist to BudgetChange if
    the API's silent-ignore starts creating projection drift.

- [ ] **Max-CPC follow-ups from security-auditor review** (logged
      during M2 Kill-switch #2; deferred as lower severity / out of
      scope for current PR):
  - DESIGN: **Auto-bidding strategy bypass** — MaxCpcCheck only
    validates explicit `new_search_bid_rub` / `new_network_bid_rub`
    in ProposedBidChange. Yandex Direct's portfolio strategies can
    override keyword-level CPCs at serving time. If M2.2 adds an
    OperationPlan carrying strategy-change ops, those need their
    own kill-switch or this one must be extended.
  - DESIGN: **Unconstrained-campaign misconfig trap** — a campaign
    absent from `campaign_max_cpc_rub` is fully unconstrained. When
    the M2.3 audit sink lands, emit a warn on first-use of such a
    campaign so configuration drift is visible.
  - DESIGN: **`load_max_cpc_policy` empty-policy silence** — a
    typo'd YAML key silently disables the entire kill-switch.
    Consider hard-failing or emitting a warn when the loaded policy
    is empty while the kill-switch is registered.
  - PERF/LOW: **O(n·m) snapshot.find()** — linear scan per update.
    Acceptable at current Direct scale but becomes relevant when
    M2.2 chains multiple checks per plan.

- [ ] **Budget-cap follow-ups from security-auditor review** (logged
      during M2 Kill-switch #1; deferred as lower severity):
  - LOW: unmatched campaign ids in `BudgetChange` list are silently
    dropped by `BudgetCapCheck._project`. Surface them as a warn-
    level annotation in `CheckResult.details` so M2.3 audit sink
    can log them.
  - MEDIUM: `load_budget_cap_policy` accepts `account_daily_budget_cap_rub: 0`
    silently — effectively disables the agent without a warning.
    Emit a warning (or hard-fail) at load time when M2.1 lands the
    full Policy loader.
  - DESIGN: `warn` CheckResult status is defined but never returned.
    Define approaching-cap thresholds (e.g. 80% / 90% of cap →
    warn) in M2.1 so the status is not dead code across the seven
    kill-switches.
  - DESIGN: agent-supplied `group` labels on `CampaignBudget` are
    trusted. M2.2 snapshot-builder must enforce that group labels
    come only from the Direct API / trusted config, never from
    agent-proposed changes.

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

- [x] **chore(deps): pin ruff version across pyproject + pre-commit** —
      replaced `"ruff>=0.6"` with `"ruff==0.15.11"` in dev extras
      and bumped `.pre-commit-config.yaml` `ruff-pre-commit` rev
      from the stale `v0.8.4` to `v0.15.11`. Three sources of
      truth (local venv / CI `pip install` / pre-commit hook)
      now resolve to the same build, closing the version-skew
      bug that had been hitting KS PRs as `ruff format --check`
      CI failures on code that passed locally. Bumping ruff is
      now a 3-file operation documented in both config comments.
- [x] **M2 Kill-switch #7 — Query drift detector** — second
      system-level gatekeeper. `QueryDriftCheck(baseline, current)`
      compares normalised query sets, blocks when
      `|new_queries|/|current|` exceeds `max_new_query_share`
      (default 0.4). Shared `_normalize_keyword` extended with
      internal-whitespace collapse (also benefits KS#3; pinned by
      `test_multi_word_negative_keyword_internal_whitespace_collapses`).
      Counter-id mismatch rejected upfront; empty baseline /
      current → warn. 26 new tests (176 safety, 299 total).
      **M2.0 complete** — all 7 kill-switches delivered and
      security-audited. Coverage 91.5%.
- [x] **M2 Kill-switch #6 — Conversion integrity** —
      `ConversionIntegrityCheck` is a system-level gatekeeper
      (no `changes` arg). Signature: `check(baseline, current)`.
      Three independently-tunable rules: absolute floor, ratio vs
      baseline, baseline-goals presence. Counter-id mismatch
      rejected upfront. `GoalConversions.__post_init__` guards
      negative / bool / non-int counts. 30 new tests (150 safety,
      273 total). Reviewed by `security-auditor` — 2 code findings
      (negative-count bypass + counter_id mismatch) closed in-PR;
      2 design items moved to Tech debt (global-gatekeeper marker
      for M2.2, warn-in-autonomous-mode policy for M2.2).
- [x] **M2 Kill-switch #5 — Budget-balance drift** —
      `BudgetBalanceDriftCheck` refuses plans that shift any
      campaign's share of active daily budget more than
      `max_shift_pct_per_day` (default 0.3, `gt=0, le=1`) vs. a
      baseline snapshot. First kill-switch with a temporal
      dimension. Reuses `AccountBudgetSnapshot` + `BudgetChange`;
      projects changes via shared `BudgetCapCheck._project`. IEEE
      754 boundary handled with `math.isclose(abs_tol=1e-14)`.
      Empty baseline → `warn` (not `ok`) to surface first-run /
      missing-backfill cases in M2.3 audit. 22 new tests (120
      safety, 244 across the suite). Reviewed by `security-auditor`
      — three findings closed in-PR (tolerance tightened, empty
      baseline warns, details payload carries baseline_total_rub);
      one MEDIUM (baseline provenance) + one DESIGN (ceiling
      warning) escalated to Tech debt.
- [x] **M2 Kill-switch #4 — Quality Score guardrail** —
      `QualityScoreGuardCheck` blocks bid *increases* on keywords
      whose QS is below the configured threshold. Policy is a narrow
      slice: `min_quality_score_for_bid_increase: int` with
      `Field(ge=0, le=10)` defaulting to 5. `KeywordSnapshot` grows
      `quality_score: int | None` and a `__post_init__` guard that
      rejects float / bool / out-of-range values. Search-before-
      network evaluation order pinned. 28 new tests (99 safety, 222
      across the suite). Reviewed by `security-auditor` — no single-
      call bypass found; 3 follow-ups (cross-call ratchet, None-
      current defer, §M2.6 trending) moved to Tech debt.
- [x] **M2 Kill-switch #3 — Negative-keyword floor** —
      `NegativeKeywordFloorCheck` refuses to resume a campaign that
      does not carry every required negative keyword. Reuses KS#1
      shapes (AccountBudgetSnapshot, BudgetChange). CampaignBudget
      grows `negative_keywords: frozenset[str]` default-empty, so
      existing KS#1/#2 tests stay green. Local `_normalize_keyword`
      applies NFC → strip → lower; case- and whitespace-insensitive
      and Unicode-canonical. Policy rejects empty/whitespace-only
      entries via a `@field_validator`. 24 new tests (71 safety, 194
      across the suite). Reviewed by `security-auditor` before
      merge — HIGH finding (NFC/NFD) and MEDIUM finding
      (empty-string DoS) closed; 4 design notes added below.
- [x] **M2 Kill-switch #2 — Max CPC per campaign** — `MaxCpcCheck`
      enforces per-campaign CPC caps on bid updates. Adds
      `KeywordSnapshot`, `AccountBidSnapshot`, `ProposedBidChange`
      (pydantic BaseModel, Field(ge=0), extra=forbid, auditor-hardened
      from day one), `MaxCpcPolicy`, `load_max_cpc_policy`. 22 new
      tests: shapes, validation, YAML loader, key-coercion contract,
      happy paths (below / at cap / no cap), blocks (search, network,
      both, duplicate ids), silent-skip unknown ids. Reviewed by
      `security-auditor` before merge — MEDIUM/LOW tests added
      (both-bids-exceed evaluation order, policy key coercion).
- [x] **M2 Kill-switch #1 — Budget caps** — `agent/safety.py` with
      `BudgetCapPolicy`, `AccountBudgetSnapshot`, `BudgetChange`
      (frozen pydantic BaseModel with Field(ge=0) on budget and
      Literal on state), `BudgetCapCheck`, `CheckResult`. YAML loader
      tolerates M2.1+ keys. `agent_policy.example.yml` shipped.
      26 tests across happy/account/group/suspended semantics +
      auditor-driven HIGH/MEDIUM fixes (negative budget, unknown
      state string, duplicate ids in changes list). Reviewed by
      `security-auditor` sub-agent before merge.
- [x] **PR-C: yadirect-agent doctor command** — four checks
      (env / policy file / Anthropic ping / Direct sandbox ping),
      coloured table output, exit 2 on any failure. Delivered as
      a clean RED → GREEN pair: failing tests + skeleton first,
      real implementations + typer wiring second. Coverage 85.7%
      → 87.0%.
- [x] **M7.1 dotyazhka** — `test_semantics.py` (20 tests covering
      normalize / _cluster_key / collect / validate_with_direct) +
      `test_bidding.py` (6 tests pinning rubles→micro conversion,
      empty-list no-op, batching). First real TDD PR: a visible
      `test:` → `fix:` pair drove a real bug fix in
      `_cluster_key`'s all-stop-words fallback (was returning the
      raw phrase; now returns a normalised key). Coverage 78.9% →
      85.7%; gate raised 78 → 80.
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
