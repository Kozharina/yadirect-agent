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

*(M2 fully shipped — see Done. Next safety work happens
inside other milestones: M3 MCP `--allow-write` gating builds on
M2's pipeline; M5 A/B testing service inherits M2 audit; M7
evals exercise the full safety surface.)*

*(M3 fully shipped — see Done.)*

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

- [ ] **Per-keyword `AccountBidSnapshot` reader for KS#2 / KS#4**
      (branch ``feat/m2-bid-snapshot-reader``) — extends
      ``models/keywords.Keyword`` with bid + productivity fields,
      extends ``DirectService.get_keywords`` to read them, and rewires
      ``BiddingService._build_bid_context`` to return a populated
      ``AccountBidSnapshot``. Without this, KS#2 (max-CPC) and KS#4
      (QS guard) defer on every bid call and the protection on the
      bid path is plan→confirm→execute + rollout_stage + audit only.
      See Tech debt entry for the full motivation.

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

- [ ] **Audit emit guards: narrow `except Exception` to `OSError`**
      (from PR M2.3a second-pass auditor ADVISORY-1; non-blocking):
      ``audit_action``'s success / failure-path emit guards swallow
      every ``Exception``, but the documented intent is "I/O
      failures don't mask the wrapped operation". A custom sink
      raising ``ValidationError`` (programmer bug — malformed
      AuditEvent in a future sink subclass) or ``TypeError`` /
      ``AttributeError`` (silent runtime error) would be hidden
      behind a structlog warning. Tighten to ``OSError`` or at
      minimum re-raise programmer-error classes. Document the gap
      between intent and implementation in the inline comment.

- [ ] **Audit module: bind ``_logger`` once at module level**
      (from PR M2.3a second-pass auditor ADVISORY-2; non-blocking
      stylistic): ``audit_action`` calls
      ``structlog.get_logger(__name__)`` at each except site rather
      than at module import. Cheap (proxy object) but inconsistent
      with the rest of the codebase. Replace with a single
      ``_logger = structlog.get_logger(__name__)`` binding next to
      the other module-level constants.

- [ ] **KS#3 missing-phrase visibility for operator triage** (from
      PR M2.3b auditor LOW): the new count-only ``CheckResult.reason``
      ("missing N required negative keyword(s)") combined with sink-
      level ``_PRIVATE_KEYS`` redaction and tool-handler
      ``_redact_details`` means an operator triaging a KS#3 rejection
      cannot see which phrases were missing from any post-hoc
      channel — the live ``CheckResult.details["missing"]`` is
      in-process only. Decide between: (a) emit phrase list to a
      structlog DEBUG line keyed by trace_id (operator greps debug
      log post-incident); (b) hash each phrase with a stable salt
      and surface hashes (operator matches against hashed lookup of
      their own policy YAML); (c) accept as-is and document the
      runbook ("re-run policy check manually with the same
      snapshot").

- [ ] **Production-path ``audit_sink`` enforcement** (from PR M2.3b
      auditor advisory): ``CampaignService`` and ``apply_plan`` both
      accept ``audit_sink: AuditSink | None = None`` for fixture
      backwards-compat. ``build_default_registry`` and the
      ``apply-plan`` CLI supply one in production today, but a new
      CLI command or service constructor could silently bypass
      audit. Either (a) flip the default to a no-op ``NullSink`` and
      require explicit opt-out, or (b) add a
      ``settings.require_audit_sink`` flag that
      ``_resolve_safety``-style raises if production marker is set
      without a sink.

- [ ] **rollout_status_cmd: stale-but-aligned state-file display**
      (from PR M2.5 auditor LOW-3): when YAML and state-file agree
      on the same stage, ``_apply_rollout_state_override`` no-ops
      and emits no info log, but ``rollout status`` still shows the
      state-file block. Either suppress the state-file block when
      no-op, or always emit ``rollout_state_resolved`` at boot.

- [ ] **rollout promote autonomy_full: type-the-stage confirmation**
      (from PR M2.5 auditor INFO): the most dangerous transition
      currently uses y/N. For prod, require typing the literal
      ``autonomy_full`` to confirm — eliminates fat-finger.

- [ ] **rollout: docs/ROLLOUT.md operator runbook** (from PR M2.5
      auditor INFO): the ``rollout`` subapp isn't in any operator-
      facing doc. Add a short docs/ROLLOUT.md covering stage
      semantics, success-gate metrics, and ``status`` / ``promote``
      workflow with examples.

- [ ] **Audit JSONL durability — fsync on emit** (from PR M2.3a
      auditor M-3): ``JsonlSink._append`` calls ``open().close()``
      which flushes Python's buffer to the OS but does not call
      ``fsync``. A power loss / SIGKILL between close-return and the
      OS-buffer flush silently loses the most recent event. Single-
      operator local use is acceptable today; for compliance /
      regulatory archival add ``f.flush(); os.fsync(f.fileno())``
      before the context manager exits, accepting the latency hit
      (50–200 µs per emit on consumer SSD).

- [ ] **Audit JSONL rotation** (from PR M2.3a hunt list): a long-
      running agent fills ``audit.jsonl`` forever. Rotation is
      out-of-scope for the data layer but should land alongside the
      first deployment that runs more than a week. Either a sidecar
      ``logrotate`` config or a built-in size-based rotator inside
      ``JsonlSink``.

- [ ] **KS#2 / KS#4 must report `skipped` (not `ok`) on empty
      bid snapshot** (auditor M2-bidding H-1): when
      ``_build_bid_context`` returns ``AccountBidSnapshot()`` with
      no keywords, ``MaxCpcCheck.check`` and
      ``QualityScoreGuardCheck.check`` iterate zero entries and
      return ``ok``. The pipeline's ``skipped_checks`` ledger
      stays empty even though no per-keyword constraint actually
      ran. Operators reading audit output get no signal that
      these checks ran vacuously. Fix by having both checks
      return a ``skipped`` result when the requested keyword
      isn't in the snapshot, and let the pipeline collect them
      into ``skipped_checks``. Land alongside (or before) the
      bid-snapshot reader so the signal is meaningful from day
      one.

- [ ] **Dedup `_infer_actor` frame walk between Campaign / Bidding
      services** (auditor M2-bidding L-1): the frame-walking
      helper is duplicated verbatim in
      ``CampaignService._infer_actor`` and
      ``BiddingService._infer_actor``. Extract into
      ``audit.infer_actor_from_frame()`` so a future tightening
      (replace frame walk with explicit kwarg threading) lands in
      one place.

- [ ] **Per-keyword AccountBidSnapshot reader for KS#2 / KS#4** —
      ``BiddingService.apply`` is now ``@requires_plan``-gated
      (M2 follow-up), but ``_build_bid_context`` returns an empty
      ``AccountBidSnapshot``. KS#2 (max-CPC) and KS#4 (quality-
      score guard) silently defer because their per-keyword
      ``current_search_bid_rub`` / ``quality_score`` fields are
      missing — that IS their documented "no current bid known →
      can't prove violation" contract, but it means the
      protection on this path today is plan→confirm→execute +
      rollout_stage + audit, not the deeper KS#2/#4 validation.
      Fix: extend ``models/keywords.Keyword`` to include the
      Direct API's bid + quality-score fields, extend
      ``DirectService.get_keywords`` to request them via
      ``FieldNames``, and have ``_build_bid_context`` populate
      ``KeywordSnapshot.current_search_bid_rub`` /
      ``current_network_bid_rub`` / ``quality_score``. Block on
      this BEFORE any operator configures aggressive
      ``campaign_max_cpc_rub`` / ``min_quality_score_for_bid_increase``
      thresholds expecting them to hold.

- [ ] **Pull per-campaign negative keywords for KS#3** — the
      pause/resume context builders currently leave
      ``CampaignBudget.negative_keywords`` empty because we don't
      yet read per-campaign negatives from the Direct API. Default
      ``Policy.required_negative_keywords`` is empty so KS#3 is a
      no-op out of the box; once the operator configures required
      negatives in YAML, **every resume will be blocked** because
      KS#3 sees zero negatives on every campaign. Fix: extend
      ``DirectService`` with a per-campaign negatives fetch (Direct
      API's ``get_keywords`` with negative-set filter) and call it
      from ``_build_resume_context``. Block on this BEFORE the
      first operator configures ``required_negative_keywords``.

- [ ] **Snapshot freshness at apply-plan time (archived-campaign
      gap)** (from PR-B1 auditor MEDIUM-3; not blocking M2.3): the
      ReviewContext serialised into `OperationPlan.review_context`
      is built from `CampaignService.list_all()` (no state filter)
      at plan creation. If the target campaign is archived between
      plan creation and `apply-plan` execution, the snapshot still
      shows it as `state=ARCHIVED, daily_budget_rub=0.0`. KS#1
      arithmetic stays correct, but the eventual
      `update_campaign_budget` wire call will fail. Consider
      either (a) refresh snapshot at apply time and re-review
      against fresh data, or (b) explicitly check `state == "ON"`
      in the per-op check.

- [ ] **Remove KS#7 query-sample privacy blocklist when M2.3 audit
      sink redacts at source** (from PR-B1 second-pass auditor
      MEDIUM): tool handler currently strips ``new_queries_sample``
      via ``_redact_details`` so raw user search queries never
      reach the LLM agent's response. Once M2.3 audit sink lands
      with structural redaction at the persistence layer, the
      tool-boundary blocklist becomes redundant defence — keep it
      until then but consider whether any other PII-prone keys
      (KS#3 negative-keyword phrases? KS#5 baseline timestamps?)
      need adding to ``_PRIVATE_DETAIL_KEYS``.

- [ ] **Quiet `policy_file_not_found` warning in unit-test
      fixtures** (from PR-B1 second-pass auditor INFO-1): the
      ``settings`` fixture in ``tests/unit/conftest.py`` points
      ``agent_policy_path`` at a non-existent ``tmp_path`` file, so
      every test that calls ``build_default_registry`` triggers a
      structlog warning. Not a security issue but masks real
      ``policy_file_not_found`` events in any future log-assertion
      tests. Fix by writing a minimal valid YAML at fixture setup.

- [ ] **Document `agent/__init__.py` public-API narrowing**
      (from PR-B1 auditor INFO-2): the empty re-exports broke a
      circular import (`services/campaigns.py → agent.executor →
      agent.__init__ → tools → campaigns`). Anyone importing
      `from yadirect_agent.agent import Agent` now gets ImportError.
      Add a note in `docs/ARCHITECTURE.md` clarifying the
      submodule-only public surface, and audit the README for any
      stale flat-namespace examples.

- [ ] **`apply-plan` concurrency / file-lock** (from PR-A auditor
      LOW, re-raised to MEDIUM by PR-B2 auditor on the live CLI
      path; not blocking part 3b2 single-operator use): two
      concurrent `yadirect-agent apply-plan <same-id>` shell
      invocations would both pass the `status != pending` check
      (the JSONL store reads before either writes), both re-review,
      both execute, both call ``pipeline.on_applied``. The
      ``set_daily_budget`` API call is idempotent against the same
      target value (Direct accepts the second), so the monetary
      blast radius is bounded — but the session TOCTOU register
      gets DOUBLE-incremented, and a future plan within the same
      pipeline session would slip past a cap that should hold.
      Acceptance for any future fix: add a regression test
      simulating concurrent ``apply_plan`` and asserting
      ``on_applied_calls == 1`` (not 2). Implementation: wrap the
      `get → status check → service_router → update_status`
      sequence in `apply_plan` with `fcntl.flock` on the JSONL
      path. ``yadirect-agent apply-plan`` docstring already
      documents single-operator-only assumption.

- [ ] **Action-string registry pinning (decorator ↔ router)** (from
      PR-B2 auditor DESIGN NOTE): the action string
      ``"set_campaign_budget"`` is hardcoded both on the
      ``@requires_plan(action=...)`` decorator at
      ``services/campaigns.py:184`` and in the CLI router at
      ``cli/main.py:_build_service_router``. If either is renamed
      without updating the other, every ``apply-plan`` of the
      affected action silently routes to the unknown-action
      branch and exits 3. Pin the relationship with either a
      shared ``ACTION_*`` constant module or a registry pattern
      (``@register_action("set_campaign_budget")`` decorator that
      both wires the @requires_plan and registers the router
      entry). Add a runtime assertion at registry-build time that
      every decorated method's action string has a corresponding
      router entry. Defer until a second decorated method
      exists — the abstraction has no value with one entry.

- [ ] **Plan-store I/O failure masking** (from PR-A second-pass
      auditor LOW NF-2; not blocking part 3b): if the JSONL append
      inside `apply_plan`'s `update_status("failed")` itself raises
      (disk full, file deleted), the new `OSError` masks the
      original executor failure. Same risk on `update_status("applied")`
      — a write failure leaves the plan in `pending` while the API
      call has succeeded, opening a double-spend window on retry.
      Fix: wrap both `update_status` calls in their own try/except,
      log the original exception at error level via structlog, then
      re-raise with `from original_exc` to preserve causality.

- [ ] **Executor logger should be structlog, not stdlib `logging`**
      (from PR-A second-pass auditor LOW NF-3; not blocking part 3b):
      the `on_applied`-failure recovery path in `apply_plan` uses
      `logging.getLogger(__name__).exception(...)` while the rest of
      the agent package uses `structlog`. Stdlib `logging` bypasses
      the contextvars binding (trace_id and friends), so the
      operator searching by trace_id after a stale TOCTOU register
      finds nothing. Replace with `structlog.get_logger(__name__)`.

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

- [x] **M2 follow-up — `BiddingService.apply` gated through
      @requires_plan; MCP denylist now empty** — closes the last
      mutating service method. ``BiddingService.apply`` runs
      through the safety pipeline + audit + rollout-stage gate;
      every bid change returns ``confirm`` (no
      ``auto_approve_bid_change`` knob) and the operator must
      run ``apply-plan`` to actually mutate. ``BidUpdate``
      converted from frozen dataclass to frozen pydantic
      ``BaseModel`` so ``OperationPlan.args`` round-trips through
      JSON for apply-plan replay. New ``_build_bid_context``
      returns an empty ``AccountBidSnapshot`` — KS#2 / KS#4
      defer until a per-keyword bid+QS reader lands (BACKLOG'd
      as a hard prerequisite before tightening max-CPC / min-QS
      thresholds). Inner API call extracted to ``_do_apply``.
      ``set_keyword_bids`` removed from MCP denylist —
      ``_MCP_WRITE_TOOLS_DENYLIST`` is now empty (mechanism
      preserved + tested via monkeypatch). Tools registry
      factory split renamed: ``_CAMPAIGN_FACTORIES`` →
      ``_GATED_FACTORIES`` (CampaignService + BiddingService);
      ``set_keyword_bids`` moved into the gated set. CLI service
      router extended with ``set_keyword_bids`` action mapping;
      ``_make_set_keyword_bids_tool`` handler now catches
      ``PlanRequired`` / ``PlanRejected`` and returns the
      structured pending/rejected response shape. After this
      PR every mutating service method across the project is
      structurally unbypassable through every entry point
      (CLI / agent loop / MCP). 8 new tests in test_bidding.py +
      handler-shape updates; 514 total green.
- [x] **M3 — MCP server (bootstrap + flag gating + Claude
      Desktop docs)** — closes §M3 entirely. New module
      ``yadirect_agent.mcp.server`` ships ``build_mcp_server`` +
      ``McpServerHandle``: thin publishing wrapper over
      ``build_default_registry`` reusing pipeline / store /
      audit_sink / @requires_plan / 7 tool handlers. Read-only
      mode (``allow_write=False``, default) hides write tools
      from the LLM entirely — defence in depth on top of
      @requires_plan. ``--allow-write`` (or env
      ``MCP_ALLOW_WRITE=true``) opts in; mutations still flow
      through plan→confirm→execute and require an out-of-band
      ``yadirect-agent apply-plan <id>`` from the operator's
      terminal. Schema preservation: each MCP tool's
      ``inputSchema`` is the pydantic ``input_model``'s
      ``model_json_schema()`` verbatim — ``extra="forbid"``
      becomes ``additionalProperties: false`` so MCP clients
      reject unknown fields before they reach our handler. New
      ``yadirect-agent mcp serve`` typer subapp with
      ``--allow-write`` flag and env fallback. ``ToolRegistry``
      gains ``__iter__`` for clean walk. Operator runbook
      ``docs/CLAUDE_DESKTOP.md`` shipped with copy-pasteable
      Claude Desktop ``mcpServers`` JSON blocks (read-only +
      write modes), full operator workflow, troubleshooting
      table, and rollout-stage promotion sequence. 10 new tests
      (7 server unit + 3 CLI smoke); 510 total green.
- [x] **M2 follow-up — pause / resume gated through @requires_plan**
      — closes the HIGH-1 finding from PR-B1 second-pass auditor.
      ``CampaignService.pause`` and ``CampaignService.resume`` now
      run through the safety pipeline AND emit audit events; the
      previous version had pipeline+store on the instance but
      methods didn't consult them, leaving resume (the primary
      KS#3 trigger) effectively ungated. New ``_build_pause_context``
      / ``_build_resume_context`` async helpers + extracted shared
      ``_build_account_budget_snapshot``. Bulk semantics
      preserved: one plan covers the whole list of ids; apply-plan
      applies all-or-none. ``set_campaign_budget`` /
      ``pause_campaigns`` / ``resume_campaigns`` tool handlers
      share new ``_pending_response`` / ``_rejected_response``
      helpers (privacy-redacted ``details``). CLI service router
      extended with ``pause_campaigns`` / ``resume_campaigns``
      mappings. With default policy, pause auto-completes (single
      shot via auto_approve_pause=True), resume requires
      operator approval (auto_approve_resume=False).
      ``BiddingService.apply`` still queued. 2 new end-to-end
      tests (pause through allow / resume through confirm) +
      handler-response shape updates; 497 total green.
- [x] **M2.5 — Staged rollout (state-file + CLI)** — closes
      §M2 entirely. New module ``yadirect_agent.rollout``
      shipping ``RolloutState`` (frozen pydantic, AwareDatetime,
      Literal stage) + ``RolloutStateStore`` (single-snapshot
      JSON read/write; corrupt-file boot-safe). New
      ``_apply_rollout_state_override`` in tools.py overrides
      ``Policy.rollout_stage`` from YAML when the state-file is
      present (logs ``rollout_state_override`` info). New
      ``yadirect-agent rollout`` subapp:
      - ``status``: shows effective stage + source (YAML default
        vs state-file override with timestamp + actor +
        previous-stage transition).
      - ``promote --to <stage> [--yes] [--actor <id>]``:
        validates target, prints transition (red WARNING for
        autonomy_full), interactive confirm by default, persists
        ``rollout_state.json`` AND emits the
        ``rollout_promote.requested|.ok|.failed`` audit envelope.
      Exit codes: 0 / 1 invalid stage / 2 declined / 3 write
      failure. Both upgrades and downgrades allowed —
      downgrade-to-shadow is the safety win after an incident.
      ``--actor`` defaults to ``getpass.getuser()``. 11 new
      tests in ``test_rollout.py`` + 2 in test_tools.py + 6 in
      test_cli.py; 489 total green.
- [x] **M2.4 — Daily-budget hard guard (env backstop)** — closes
      §M2.4. ``build_safety_pair`` now applies an env-level
      backstop on the account budget cap: every Policy is built
      with ``budget_cap.account_daily_budget_cap_rub =
      min(yaml_cap, settings.agent_max_daily_budget_rub)``. The
      env wins when a YAML drift / typo / leaked-from-dev cap
      would loosen the deployment ceiling — operators set the
      env at deploy time and trust the file system to honour it.
      Implementation is a pure helper ``_apply_env_backstop`` that
      returns the original Policy unchanged when the YAML is
      already tighter, else a deep-copied Policy. Logs a structlog
      ``env_backstop_tightening_account_cap`` warning whenever it
      tightens (yaml/env/effective values included so the operator
      can debug "why is the agent rejecting valid budgets").
      Single source of truth: KS#1 BudgetCapCheck stays env-
      unaware; the env is just one more input into the cap. Three
      mutating actions covered transitively (budget bump / resume
      / archive); bid increases correctly do not affect the cap.
      4 new tests in ``TestEnvBackstop``; 468 total green.
- [x] **M2.3b — Audit sink wiring** — closes §M2.3.
      ``CampaignService.set_daily_budget`` and ``apply_plan`` now
      emit ``set_campaign_budget.requested|.ok|.failed`` and
      ``apply_plan.requested|.ok|.failed`` through the shared
      ``JsonlSink`` constructed in ``build_safety_pair`` (3-tuple
      now). Actor inferred via bounded frame walk on the service:
      ``_applying_plan_id`` in any caller frame → ``human``,
      otherwise ``agent``. ``apply_plan`` always emits actor=
      ``human``. ``audit_sink`` is opt-in by sink presence; CLI /
      registry threads through the live JsonlSink, fixtures /
      tests can omit. KS#1 ``group`` decision: accept-as-is (label
      is structural identifier for cap-grouping, not advertiser-
      facing). KS#3 ``reason`` interpolation: replaced join with
      count (``"missing N required negative keyword(s)"``); the
      ``details["missing"]`` list keeps the phrases for in-process
      inspection but the audit sink strips the key. 9 new tests
      (3 service + 4 executor + 2 happy/backwards-compat); 462
      total green; mypy + ruff clean.
- [x] **M2.3a — Audit sink module (data layer)** — first slice of
      §M2.3. ``src/yadirect_agent/audit.py`` ships ``AuditEvent``
      (frozen pydantic, ``extra="forbid"``,
      ``actor`` Literal{agent,human,system}), an ``AuditSink``
      Protocol so a future deployment can swap JSONL for Kafka /
      Postgres without touching service code, and a default
      ``JsonlSink`` that ``asyncio.to_thread``-wraps the blocking
      ``open(..., "a")`` so the agent's event loop never stalls
      on disk I/O. ``audit_action`` async context manager emits
      ``<action>.requested`` on entry and ``<action>.ok`` (with
      ``ctx.set_result()`` + ``ctx.set_units_spent()`` payloads)
      or ``<action>.failed`` (preserving partial result +
      appending error_type/error_message + re-raising the
      original) on exit. Sink-level redaction via
      ``redact_for_audit`` walks dicts/lists and drops
      ``_PRIVATE_KEYS = {"new_queries_sample"}`` — same blocklist
      the tools-layer response redactor uses (PR #25), defence in
      depth. 16 new tests; 450 total green. Wiring into services
      lands in M2.3b.
- [x] **M2.2 part 3b2 — `apply-plan` CLI** — closes M2.2.
      ``yadirect-agent apply-plan <id>`` re-reviews the stored plan
      against its original ReviewContext, dispatches via a service
      router (currently mapping ``set_campaign_budget`` →
      ``CampaignService.set_daily_budget``), and prints a green
      ``applied`` line on success. Cron-friendly exit codes:
      0 applied, 1 preconditions failed (unknown id / not pending /
      no review_context), 2 re-review rejected, 3 underlying
      service raised. ``build_safety_pair`` promoted from
      ``_build_safety_pair`` so the CLI resolves Policy from the
      same path as the agent's tools registry, guaranteeing that
      re-review at apply time uses the same thresholds the
      original decision was made under. 4 new CLI smoke tests
      (12 in test_cli.py); 432 total green.
- [x] **M2.2 part 3b1 — service wiring (CampaignService + tools
      registry)** — first real consumer of the part-3a executor
      infrastructure. ``CampaignService.__init__`` accepts ``pipeline``
      / ``store`` keyword-only optional, with ``_resolve_safety``
      raising ``RuntimeError`` rather than silently bypassing.
      ``set_daily_budget`` decorated with ``@requires_plan`` +
      ``_build_set_budget_context`` async builder that reads the
      current ``AccountBudgetSnapshot`` via ``list_all()``.
      ``build_default_registry`` constructs a single shared
      ``SafetyPipeline`` + ``PendingPlansStore`` per registry build
      so the cross-tool TOCTOU register survives one agent run;
      Policy resolved from ``settings.agent_policy_path`` if present,
      otherwise a default seeded from
      ``settings.agent_max_daily_budget_rub``. ``set_campaign_budget``
      tool handler catches ``PlanRequired`` / ``PlanRejected`` and
      surfaces ``{status: "pending" | "rejected" | "applied", ...}``
      so the agent can relay the next step to the user.
      ``agent/__init__.py`` no longer eagerly re-exports submodules
      — eager re-exports formed an import cycle the moment
      ``services/campaigns.py`` started importing
      ``agent.executor``. 26 tests in test_tools.py (was 24), 13 in
      test_campaigns.py (was 10), 419 total green; mypy + ruff
      clean. ``apply-plan`` CLI lands in part 3b2.
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
