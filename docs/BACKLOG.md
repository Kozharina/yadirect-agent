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

- [ ] **M2.2 part 3b — service wiring + `apply-plan` CLI** — final
      slice of §M2.2. Build on the executor infrastructure from part
      3a: implement `_resolve_safety()` on `CampaignService`,
      decorate `set_daily_budget` with `@requires_plan` and a real
      `context_builder` (reads current `AccountBudgetSnapshot` via
      `list_all()`). Add `yadirect-agent apply-plan <id>` typer
      command that wires `apply_plan()` against a service-router
      mapping `action` strings to service methods. Smoke-test the
      CLI; security-auditor review before merge.
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

- [ ] **M2.2 part 3 executor must-haves** (from SafetyPipeline
      second-pass auditor review; block apply-plan merge):
  - **Executor must call `SafetyPipeline.on_applied(context)`
    exactly once after a successful API write**, and must NOT call
    it on failure or timeout. The session TOCTOU register (max
    approved bid per keyword) degrades silently to the per-snapshot
    ceiling alone if this contract breaks. Acceptance test: write a
    regression that exercises the failure path — plan allowed,
    executor raises, next review of the same keyword at a higher
    bid must NOT slip past the session cap.
  - **Plan must carry the exact `ReviewContext` that produced the
    decision** (not a rebuilt one at execute time), so
    `on_applied` records bids against the same snapshot the
    decision was made on. Alternatively, persist the minimal
    identity (keyword_id → approved ceiling) inside
    `OperationPlan.args` and reconstruct at apply time.

- [ ] **M2.2 pipeline must-haves** (from M2.1 auditor review; block
      M2.2 merge, not just landing):
  - **`rollout_stage` enforcement** — today the field is stored but
    never consulted. M2.2 pipeline runner MUST read it and refuse
    ops that exceed the stage's permissions (shadow: read-only;
    assist: pause/negatives/bid-±10%; autonomy_light: bid-±25%,
    budget-±15%; autonomy_full: everything). A silent stored-but-
    unchecked field is worse than no field — operators expecting
    `autonomy_full` get `shadow` behaviour with no error, or vice
    versa.
  - **`forbidden_operations` comparator normalisation** — the list
    is now normalised to lowercase snake_case at policy load. M2.2
    pipeline MUST normalise the operation name at the call site
    before the `in forbidden_operations` check, or the guard fails
    silently on a case-different call.
  - **`auto_approve_negative_keywords` default decision** — auditor
    flagged that the `True` default is wider than the comment
    claims (a bad negative can suppress all relevant traffic, not
    just the intended subset). Decide in M2.2 whether to: (a) flip
    to False by default, (b) keep True but add a size-limit guard
    (`len(new_negatives) > N requires_confirmation`), or (c)
    explicitly document the trade-off.

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

- [x] **M2.2 part 3a — `@requires_plan` decorator + `apply_plan`
      executor (infrastructure)** — `agent/executor.py`. Decorator
      hooks `SafetyPipeline.review` into async service methods with
      three exit paths (allow → run + on_applied; confirm → persist
      + raise `PlanRequired`; reject → raise `PlanRejected`) and an
      `_applying_plan_id` escape hatch so apply-plan re-entry skips
      the pipeline. `apply_plan(plan_id, ...)` validates
      preconditions (status pending, review_context present),
      re-reviews against the original snapshot, routes through a
      caller-supplied `service_router`, and enforces the on_applied
      invariant from BACKLOG (success path is the only caller; the
      executor-failure path skips on_applied and marks the plan
      `failed`). `OperationPlan` extended with
      `review_context: dict | None` and a `failed` status; pipeline
      gains `serialize_review_context` / `deserialize_review_context`
      via pydantic `TypeAdapter` so frozen-dataclass snapshots
      round-trip without migrating to BaseModel. 11 new executor
      tests (all four decorator paths + five apply_plan paths),
      7 new model + serde tests; 405 total green. Service wiring +
      CLI command land in part 3b.
- [x] **M2.2 pipeline — SafetyPipeline orchestrator** — second
      slice of §M2.2. `SafetyPipeline.review(plan, context)`
      aggregates all 7 kill-switches + §M2.1 gatekeepers into a
      single `allow | confirm | reject` decision with a skipped-check
      ledger. Stages: forbidden_operations → rollout_stage allow-list →
      read-only shortcut → required-snapshot guard → system
      gatekeepers (KS#6/7) → per-op checks (KS#1/2/3/4/5) → session
      TOCTOU → approval tier. Required-snapshot guard explicitly
      rejects mutating actions when the caller did not supply the
      data the relevant kill-switch needs (prevents the auditor's
      CRITICAL empty-context bypass). `on_applied(context)` is a
      separate post-execution callback that records the session
      TOCTOU state (`max_approved_bid`) so a failed executor does
      not poison the session cap. Default-confirm posture: the
      `_AUTO_APPROVABLE_ACTIONS` whitelist (pause/resume/
      add_negative_keywords) is the ONLY set that auto-allows;
      every other mutating action returns `confirm` until an
      explicit auto-approve knob lands. 36 new pipeline tests
      (forbidden / rollout / read-only / required-snapshot /
      gatekeepers / per-op / tiers / session / skipped; 386 total).
      `security-auditor` pre-merge review: CRITICAL + 2 HIGH + 2
      MEDIUM + 1 LOW all addressed before green tree.
- [x] **chore(deps): pin ruff version across pyproject + pre-commit** —
      replaced `"ruff>=0.6"` with `"ruff==0.15.11"` in dev extras
      and bumped `.pre-commit-config.yaml` `ruff-pre-commit` rev
      from the stale `v0.8.4` to `v0.15.11`. Three sources of
      truth (local venv / CI `pip install` / pre-commit hook)
      now resolve to the same build, closing the version-skew
      bug that had been hitting KS PRs as `ruff format --check`
      CI failures on code that passed locally. Bumping ruff is
      now a 3-file operation documented in both config comments.
- [x] **M2.2 data layer — OperationPlan + PendingPlansStore** —
      first slice of §M2.2. `OperationPlan` frozen pydantic model
      (plan_id / created_at / action / resource_type / resource_ids
      / args / preview / reason / status / trace_id).
      `PendingPlansStore` is an append-only JSONL: `append`,
      `list_pending`, `get`, `update_status`. Status updates append
      a new row rather than rewrite, so the file doubles as a
      tamper-evident audit trail until M2.3 audit sink ships.
      Readers collapse-by-id keeping latest. Robust to blank lines
      and corrupt rows. CLI `yadirect-agent plans list [--all]` and
      `plans show <id>` render pending/all plans via rich table and
      per-plan detail view. `generate_plan_id()` gives URL-safe
      16-hex-char ids. 28 new tests (plans + CLI smoke; 350 total).
      Orchestrator / @requires_plan decorator / apply-plan
      executor land in the next PR.
- [x] **M2.1 — Unified Policy schema** — single frozen pydantic
      `Policy` aggregates all 7 slice-policies plus §M2.1's four
      groups (approval tiers, per-op thresholds, forbidden_ops,
      rollout_stage). YAML stays flat; `load_policy(path)` routes
      each key to its slice and rejects unknown keys loudly.
      64 KiB file-size guard against billion-laughs YAML. Seven
      individual `load_*_policy` helpers remain for backwards
      compat. `forbidden_operations` has a `field_validator` that
      rejects blank entries and normalises case/whitespace so the
      M2.2 pipeline can do case-insensitive lookup. Sync-test
      pins `_*_KEYS` frozensets == slice `model_fields` to catch
      the "add field, forget key map" maintenance trap. 23 new
      tests (199 safety / 322 total). Reviewed by
      `security-auditor` — MEDIUM + 3 LOW + 1 DESIGN all closed
      in-PR; `auto_approve_negative_keywords` default explicitly
      flagged for M2.2 decision.
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
