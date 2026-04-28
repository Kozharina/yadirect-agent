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

Ordered by **user journey phase** (see
[`docs/OPERATING.md`](./OPERATING.md) → "User journey" and
[`docs/TECHNICAL_SPEC.md`](./TECHNICAL_SPEC.md) → "Путь пользователя").
The product target is **a media-buyer-replacement agent for Anna**
(non-developer account owner). M0–M3 done; M15 is the gate — without
it, nothing else matters because Anna can't get past install.

### 🚪 Phase 0 + Phase 1 (Discovery + Shadow) — release 0.2.0

**This is the top of the queue.** Until M15 ships, the product is
demo-only, technically; it cannot be handed to a non-developer.

- [ ] **M15 — Frictionless onboarding** (§M15): PyPI release,
      `install-into-claude-desktop`, standard OAuth flow with
      localhost callback + keyring, conversational MCP onboarding,
      `--no-llm` rule-based mode, built-in scheduler
      (LaunchAgent/systemd/Task Scheduler). Acceptance:
      time-to-first-value ≤ 10 min on a clean machine, **without
      Anthropic key**. Smoke-tested in CI.
- [x] ~~**M20 — Human-readable rationale (slice 1)**~~ — shipped,
      see Done. Model + store + soft-optional emission +
      ``yadirect-agent rationale show/list`` CLI.
- [ ] **M20 — Hard-required emission** (slice 2): flip the
      ``@requires_plan`` rationale kwarg from soft-optional to
      hard-required (caller MUST pass ``rationale=``, otherwise the
      decorator raises). Land after every existing
      ``@requires_plan`` caller (CampaignService.set_daily_budget,
      pause/resume, BiddingService.apply) is updated to pass a
      meaningful Rationale. Without this, shadow-week calibration
      remains optional rather than guaranteed.
- [ ] **M20 — `explain_decision` MCP tool** (slice 3): mirror of
      ``rationale show`` exposed over MCP so a Claude Desktop / Code
      session can ask "why did you do X?" and get the recorded
      rationale verbatim, not a fresh confabulation.
- [ ] **M20 — auto-populated `policy_slack`** (slice 4): every
      check in the safety pipeline emits its slack (distance to
      threshold) into ``CheckResult.details``; the decorator pulls
      these out and merges into ``Rationale.policy_slack``
      automatically. Today the caller fills it manually.
- [ ] **M21 — Cost tracking** (§M21, promoted from Ideas): per-call
      tokens + RUB capture, `agent_monthly_llm_budget_rub` knob,
      auto-degrade to `--no-llm` when budget exhausted, `cost
      status` surface. **Required before autonomy** — otherwise LLM
      spend creeps invisibly and the agent silently dies at
      month-end.
- [x] ~~**M6 (basic) — Metrika reporting**~~ — shipped, see Done.
- [x] ~~**M15.5.1 — Account health check (basic rules)**~~ — shipped,
      see Done. Two rules + ``yadirect-agent health`` CLI.
- [ ] **M15.5.2-6 — Health check rule expansion**: low-CTR rule
      (needs impressions from Direct reports), rejected-ads /
      rejected-keywords rule (needs Direct ad/keyword status
      readers), CTR-drift rule (needs week-over-week comparison
      = small history store), MCP tool ``account_health()`` mirror,
      ``@requires_llm`` decorator pattern for tools that gate on
      Anthropic key presence. Each is a separate small PR.

### 🛡️ Phase 2 (Assist) — release 0.3.0

Anna is in assist; the agent does reversible work, asks for
mutating work via tappable approvals.

- [ ] **M18 — Notifications & approvals** (§M18): Telegram /
      Slack / email sinks, inline-keyboard Apply/Reject/Why
      cards, HMAC-signed callback_data, 24h plan timeout,
      `notify setup telegram` wizard. **Phase 2 is impossible
      without this** — terminal-only approval is unrealistic
      for a real user.
- [ ] **M19 — Rollback / time machine** (§M19): per-run snapshot
      of dangerous fields (budgets, statuses, strategies, bids,
      adjustments), `rollback --to=<run_id>` (re-uses safety
      pipeline — rollback is itself a mutation), conversational
      `rollback_last_run()` MCP tool, conflict-handling for
      changes overwritten since the run.
- [ ] **M4 — real Wordstat** (§M4): provider protocol, Wordstat API
      impl (gated by real access), KeyCollector CSV bridge,
      embeddings-based clustering, negative-keyword cleaner, upload
      respecting Direct's 200-keywords-per-group cap.
- [ ] **M5 — A/B testing service** (§M5): `AbTest` model,
      Mann-Whitney U for CPA/ROAS, bootstrap CIs, `conclude`
      auto-pauses losers. **More useful once M4 lands.**
- [ ] **M6 (full) — alerts** (§M6.3): `services/alerts.py`,
      `alerts.jsonl`, threshold rules surfaced via M18.
- [ ] **M11 — Bid strategies** (§M11): typed strategy models,
      `set_strategy` under `@requires_plan`, `evaluate` recommender,
      trigger-based switches with KS#11 churn limit.
- [ ] **M17 — Competitive intelligence (API only)** (§M17):
      `auctionperformance.get` (or `reports`-based fallback),
      position history + competitor pressure, integrated into
      M20 rationale ("ставка не сработала, потому что доля
      показов упала с 62% до 41%").

### 🤖 Phase 3 (Autonomy) — release 0.4.0

Anna doesn't open Direct. Silence = success.

- [ ] **M8 — Creatives lifecycle** (§M8): `services/creatives/*` —
      generator (multi-hook), moderation poll + auto-repair,
      diversity guard, creative A/B (extends M5), `BusinessProfile`
      schema, KS#8 compliance check. **Depends on**: M5.
- [ ] **M9 — Audiences & targeting** (§M9): Audience API client,
      Metrika segments wrapper, look-alike + retargeting lists,
      bid-modifier service, KS#9 adjustment ceiling.
- [ ] **M10 — Budget planning & pacing** (§M10): monthly planner
      (marginal-elasticity allocation), daily pacing job, forecast
      with bootstrap CI, KS#10 pacing emergency stop.
      **Depends on**: M6 full.
- [ ] **M12 — Stakeholder reporting** (§M12): weekly + monthly
      Markdown reports, LLM-distilled insights (gated on
      Anthropic key — degrades to numbers-only without),
      Jinja templates, CLI + MCP delivery. **Depends on**: M6, M10.
- [ ] **M13 — Account health monitoring** (§M13): daily health
      check (rejected ads, lost-impression-share, dead adgroups,
      CTR drift), auto-repair via M8.2, `doctor account` CLI.
      **Depends on**: M8.
- [ ] **M16 — Calendar & seasonality** (§M16): event calendar,
      pre/post-event budget bumps via apply-plan, anomaly
      sensitivity profiles per event. Without this, the agent
      panics on Black Friday.

### 🏢 Optional — agency mode

- [ ] **M14 — Multi-account / agency mode** (§M14): per-client
      `Settings`, per-client policy file, per-client audit log,
      `agency status` CLI. **Only ship if** the product becomes
      an agency tool. Defer until there's a second real client.

### 🧪 Cross-cutting

- [ ] **M7.2 expansion — agent evals dataset**: 10–20 typed tasks
      driven through `tests/evals/` per-PR. Today there are 3
      starter evals; needs broader coverage as M4–M21 ship so
      regressions in agent reasoning surface as red.

## In progress

- [ ] **M15.1 — PyPI release** (§M15.1, branch
      `feat/m15-1-pypi-release`). First slice of M15 (frictionless
      onboarding). Polish ``pyproject.toml`` metadata for PyPI
      (urls, classifiers, keywords, license file), add
      ``.github/workflows/release.yml`` triggered on
      ``v*.*.*`` tags that builds sdist+wheel and publishes via
      PyPI Trusted Publishing (OIDC — no PyPI token in secrets),
      verify local build succeeds, document the ``pip install
      yadirect-agent`` path in README + OPERATING.md. **Blocked
      on a manual one-time human action**: registering this
      project as a Trusted Publisher at pypi.org. Workflow will
      be in place but no release tag pushed in this PR — first
      tag is a separate operator action after the workflow
      lands.

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

- [ ] **MetrikaService report pagination** (M6 follow-up):
      ``/stat/v1/data`` returns up to 100k rows by default. For an
      account with thousands of keywords/campaigns over a long
      window the report can exceed the cap and silently truncate.
      Today ``get_report`` ignores the response's ``total_rows``
      field and never paginates; for ``account_overview`` (one row
      per campaign, no keyword breakdown) this is fine, but as
      soon as a future caller groups by keyword or search query
      we'll silently drop data. Fix when the first such caller
      lands: read ``total_rows``, page via ``offset`` until
      drained, surface a warning in audit if a single page is at
      the cap. Defer until measurement says we're hitting it.

- [ ] **MetrikaService TSV / dimension-id types** (M6 follow-up):
      ``account_overview`` accepts campaign id as int OR numeric
      string from Metrika's dimension envelope, depending on which
      report endpoint version answered. Right now we accept both
      and skip everything else. We should pin the tested behaviour
      against an actual sandbox response (currently both paths are
      synthetic in tests) before the M15.5 rule-based health check
      starts depending on it for real-money decisions. Add a VCR
      cassette against ``api-metrika.yandex.net`` once we have a
      working sandbox token — runs gated by ``METRIKA_SANDBOX``
      env var, scrubbed of OAuth tokens.

- [ ] **`Policy.require_baseline_timestamp` knob for fail-closed
      snapshot freshness** (auditor M2-snapshot-age first-pass
      followup): the staleness check in ``apply_plan`` is fail-OPEN
      on ``baseline_timestamp=None`` so legacy plans that predate
      the timestamp rollout (or any future context builder that
      doesn't yet stamp it) keep applying. Threat-model gap: an
      attacker who can write the JSONL plan store can set
      ``baseline_timestamp: null`` on a corrupt row and bypass the
      staleness gate entirely. Single-operator local-trust threat
      model (acknowledged in the file-lock LOW) makes this LOW
      today, but becomes a blocker for any multi-operator or
      remote-store deployment. Fix: add
      ``Policy.require_baseline_timestamp: bool = Field(default=False)``;
      when True, a missing timestamp at ``apply_plan`` time raises
      ``StaleSnapshotError`` (or a sibling) and the plan transitions
      to ``failed``. Test both branches. Defer until either the
      threat model widens or every context builder is verified to
      stamp reliably across a release cycle.

- [ ] **`AccountBidSnapshot.find` O(n²) at large bulk sizes** (auditor
      M2-bid-snapshot second-pass NEW LOW): now that the bid snapshot
      is populated from a real API call, a single ``apply`` with N
      keyword updates triggers N linear ``find`` scans over a list of
      up to N entries. ``Policy.max_bulk_size`` defaults to 50 so the
      cost is harmless today (50 × 50 = 2 500 comparisons per call).
      If the bulk ceiling is ever raised — ``max_bulk_size = 500``
      means 250 000 comparisons — the cost grows unnoticed inside the
      hot path. Fix: when the snapshot grows past a threshold (say
      100 entries), build a ``dict[keyword_id, KeywordSnapshot]`` once
      and look up by id. Lazy: only convert when ``len(keywords) >
      threshold``. Defer until the bulk ceiling actually moves.

- [ ] **Audit redaction for live bid values in CheckResult.details**
      (auditor M2-bid-snapshot LOW): ``QualityScoreGuardCheck`` and
      ``MaxCpcCheck`` emit ``current_rub``, ``proposed_rub`` and
      ``cap_rub`` into ``CheckResult.details``. These flow through
      ``SafetyDecision.blocking_checks`` → ``PlanRejected.blocking``
      → audit log (M2.3) and agent tool responses. Bid values on
      competitor brand keywords or niche product keywords are
      commercially sensitive; exposing them to the LLM agent
      violates minimum-information-exposure. Fix: extend
      ``audit._PRIVATE_KEYS`` with ``current_rub`` / ``proposed_rub``
      / ``cap_rub``, OR introduce a ``details_for_audit`` /
      ``details_for_agent`` split in ``CheckResult`` so the operator-
      facing audit retains the values for triage while the
      LLM-facing tool response strips them.

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
- [x] ~~**Cost tracking**~~ — promoted to **M21** in Active queue
      (Phase 0+1 release 0.2.0). Tokens-per-turn + RUB cost capture,
      `agent_monthly_llm_budget_rub` ceiling, auto-degrade to
      `--no-llm` when budget exhausted.
- [ ] Project-local sub-agent `yadirect-safety-auditor` — preloaded
      with `PRIOR_ART` + `TECHNICAL_SPEC §M2` + `ARCHITECTURE`,
      reviewed against every safety-layer PR.
- [ ] Agent **evals** dataset: 10–20 typed tasks ("pause all campaigns
      with CTR < 0.5%", "raise bids on the top 5 converting keywords
      by 20%"), run per-PR, metrics: iterations, tokens, correctness.

## Done

Last 10 items (newest at top). Older items are available via
`git log -p docs/BACKLOG.md`.

- [x] **M20 — Human-readable rationale (slice 1)** (§M20, Phase 0+1,
      release 0.2.0). Foundation for the rationale layer that makes
      shadow-week calibration honest. New ``Rationale`` model with
      ``InputDataPoint`` (timestamped data + source attribution) and
      ``Alternative`` (rejected option + cause); ``Confidence`` enum
      (low/medium/high, defaults to medium so callers don't
      accidentally claim high). Summary capped at 500 chars to enforce
      one-to-two-sentence discipline. ``RationaleStore`` JSONL
      append-only sibling to PendingPlansStore — same operational
      contract: tamper-evident on disk, last-write-wins on read,
      defensive parsing of corrupt lines, structlog warning emitted
      once per scan. ``@requires_plan`` decorator gains a soft-optional
      ``rationale=`` kwarg; ``_resolve_rationale_store`` lookup via
      ``getattr`` keeps legacy services that landed before M20 working
      without a Protocol-level break change. Path semantics: persist
      on allow + confirm, skip on reject (rejection has no decision-
      to-act-on; audit sink captures it), skip on apply-plan re-entry
      (rationale already recorded at proposal time, re-emit would
      duplicate or contradict). ``decision_id`` is overwritten with
      ``plan.plan_id`` so caller-provided ids cannot diverge from the
      plan they describe. CLI subapp ``yadirect-agent rationale
      show <id> [--json] | list [--days N] [--campaign ID]``. Renderer
      separated into ``cli/rationale.py``, all operator-set free-text
      fields ``_rich_escape``'d (mirrors M15.5.1 HIGH-1 hardening).
      45 new unit tests (16 model + 11 store + 13 emission + 9 cli);
      736 total green. Out of scope: hard-required emission (after
      all callers update), MCP ``explain_decision`` tool, auto-populated
      ``policy_slack`` from safety pipeline, notifications/digest
      integration (M20.4, blocked on M18).
- [x] **M15.5.1 — Account health check (rule-based, no LLM)**
      (§M15.5, Phase 0+1, release 0.2.0). First user-visible
      product surface that doesn't require an Anthropic API
      key — deterministic ``HealthCheckService`` consuming M6's
      ``account_overview`` and applying rule classes. Two rules
      shipped: ``BurningCampaignRule`` (HIGH severity, cost > 50
      RUB AND conversions == 0 with goal_id set) and
      ``HighCpaRule`` (WARNING severity, cpa_rub > target with
      ≥5 conversions and ``Settings.account_target_cpa_rub``
      configured). Both rules respect the M6 ``cpa_rub is None``
      contract — None means undefined, never infinity, so a
      regression can't silently nuke burning campaigns through
      the high-CPA path. New ``health.py`` model module
      (``Severity``, ``Finding``, ``HealthReport``,
      ``default_window``); new
      ``Settings.account_target_cpa_rub: float | None``. New
      ``yadirect-agent health`` CLI command with
      ``--days``/``--goal-id``/``--json`` options and exit code
      1 on HIGH findings (cron-alertable). Renderer separated
      into ``cli/health.py`` for cleanliness. 34 new unit tests
      (11 model + 15 service + 8 cli); 675 total green. Out of
      scope (deferred to M15.5.2-6): low-CTR (needs impressions),
      rejected-ads/keywords (needs Direct reports), CTR drift
      (needs history), MCP tool, ``@requires_llm`` decorator.
- [x] **M6 (basic) — Metrika reading** (§M6, Phase 0+1, release
      0.2.0). Three Metrika endpoints
      (`MetrikaService.get_goals`, `get_report`,
      `get_conversion_by_source`) with retry, error mapping
      (AuthError / ValidationError / RateLimitError /
      ApiTransientError), and Authorization header validated to
      use the Metrika token. New `services/reporting.py` with
      `ReportingService.campaign_performance` (campaign-level
      Direct↔Metrika join via ``ym:ad:directCampaignID==`` filter,
      single Metrika query, all data sourced from Metrika's Direct
      integration) and `account_overview` (batch view grouped by
      ``ym:ad:directCampaignID``, no filter, defensive parsing of
      mixed-type id field). New ``yandex_metrika_counter_id``
      Settings knob (optional, ``ge=1``); ConfigError with
      operator-pointing message when missing. ``cpa_rub`` and
      ``cr_pct`` contract enforced centrally via ``_compute_cpa`` /
      ``_compute_cr_pct``: None whenever undefined (zero
      conversions / zero clicks / zero cost), never 0 or
      infinity — the contract any future rule-based filter
      (M15.5) must respect. ``DateRange`` invariant (end >=
      start) on construction. 44 new unit tests
      (12 model + 18 client + 14 service); 631 total green.

- [x] **Audit emit guards narrowed to OSError** (auditor M2.3a
      ADVISORY-1). ``audit_action``'s emit-guards now distinguish
      I/O failures (swallowed, log, preserve outcome) from
      programmer bugs (surfaced, never silently masked). Success
      path catches only ``OSError``; programmer errors propagate
      so the operator sees a broken sink immediately rather than
      discovering it weeks later via reconciliation. Failure path
      catches ``OSError`` (warning log) and ``Exception``
      (error-level log via ``structlog.exception(...)``) but
      ALWAYS re-raises the original wrapped-operation exception
      via bare ``raise`` — sink bugs must never replace the
      caller's API failure as the operator's debugging path. 2 new
      tests pin programmer-error propagation on the success path
      and original-exception preservation on the failure path
      under sink-side TypeError. 586 total green.
- [x] **`_infer_actor` dedup → `audit.infer_actor_from_frame()`**
      (auditor M2-bidding L-1). Both ``CampaignService`` and
      ``BiddingService`` had a byte-identical 8-frame walker
      matching the @requires_plan ``wrapper`` closure with
      ``_applying_plan_id`` in its locals. Extracted into a single
      module-level helper with a comprehensive docstring capturing
      the auditor HIGH lesson (match only the canonical
      ``wrapper`` closure name; the kwarg in any other frame
      MUST NOT flip the verdict). Frame-walk semantics unchanged;
      future tightening (e.g. replacing the walk with explicit
      kwarg threading through the decorator) now lands in one
      place. 5 new helper unit tests pinning all four contract
      branches + the 8-frame depth ceiling. 584 total green.
- [x] **CLI: `--state` filter on `list-campaigns` actually applies**
      — surfaced by a project-wide audit. The flag was silently
      ignored: when ``state is not None`` the CLI branched to
      ``service.list_active()`` which hardcodes ``[ON, SUSPENDED]``
      regardless of the requested value. ``yadirect-agent
      list-campaigns --state OFF`` returned ON+SUSPENDED rows with
      no indication anything was wrong. Fix: always fetch via
      ``list_all()`` and filter client-side, validate against
      ``CampaignState`` enum at the CLI boundary so typos error
      loudly. Case normalisation (``--state off`` ≡ ``--state OFF``).
      3 new tests pin the filter / case / invalid-value contracts;
      579 total green.
- [x] **M7.2 — agent evals framework (first PR)** — eval runner
      skeleton + ``EvalResult`` metrics shape + 3 starter evals
      covering happy path (pause low-CTR campaigns), reject path
      (budget cap exceeded → ``status="rejected"`` returned to
      LLM, no API call, no retry loop), confirm path (bid change
      → ``status="pending"`` with ``next_step`` apply-plan
      instruction relayed to operator). ``make evals`` target
      runs only the eval suite verbose; ``make test`` picks them
      up alongside unit tests since they're cost-free. Wires
      ``FakeAnthropic`` + a unified ``FakeDirectService`` that
      covers every API method any tool handler may call;
      ``patch_direct_service`` helper centralises the
      monkeypatch-three-import-sites gotcha so eval files stay
      short. Each eval pins tool sequence + tool-result shape
      + iteration count + final-text content the operator sees.
      Subsequent PRs add evals incrementally as M4 / M5 / M6
      features land. 576 total green; mypy strict; ruff clean.
- [x] **M2 follow-up — `max_snapshot_age_seconds` enforcement at
      apply-plan** — closes the deferred half of the auditor
      M2-bid-snapshot HIGH-2 (and M2-ks3-negatives HIGH-2)
      findings. ``Policy.max_snapshot_age_seconds`` (default 300 s,
      ``ge=1``) added to the flat-YAML loader's top-level keys so
      operators can override per agent_policy.yml.
      ``_apply_plan_inner`` now reads
      ``context.baseline_timestamp`` AFTER plan-state validation
      and BEFORE the re-review: a plan whose snapshot is older
      than the ceiling raises ``StaleSnapshotError``, plan
      transitions to ``failed`` (terminal), executor never runs,
      ``on_applied`` not called. Audit emission preserved
      (``apply_plan.failed`` carries ``error_type:
      StaleSnapshotError`` + the human-readable age in the
      message body).
      Fail-open on ``baseline_timestamp=None`` keeps legacy plans
      applicable; defaulting to a fail-closed knob is BACKLOG'd
      as ``Policy.require_baseline_timestamp``. Negative age
      (future timestamp from NTP jitter or corrupt JSONL row)
      clamps to zero so a far-future ``baseline_timestamp`` cannot
      trivially bypass the gate via the ``negative > max_age``
      route — auditor second-pass blocker, fixed in the same PR
      with a year-2099 regression test. After this PR, all four
      kill-switch paths (KS#1 set_daily_budget, KS#1+KS#3
      pause / resume, KS#2+KS#4 set_keyword_bids) honor the
      same staleness contract end-to-end. 7 new tests
      (3 policy + 4 executor); 573 total green.
- [x] **`DailyBudget` API alias fix** — added ``alias="Amount"`` /
      ``alias="Mode"`` to ``DailyBudget`` so ``Campaign.model_validate``
      against the real wire JSON shape populates ``daily_budget``
      end-to-end. Pre-fix the inner field validation raised on
      every real ``DirectService.get_campaigns`` response —
      hidden across 566 tests because every fixture constructed
      ``DailyBudget(amount=...)`` directly via the snake_case
      constructor. Caught before the first sandbox integration
      run courtesy of the KS#3 reader's end-to-end PascalCase
      tests trying to reach the same shape. 3 new tests; 566
      total green.
- [x] **M2 follow-up — Per-campaign negative keywords reader for
      KS#3** — closes the footgun that would have blocked every
      resume the moment an operator configured
      ``required_negative_keywords`` in agent_policy.yml.
      ``Campaign`` model gains ``negative_keywords: list[str]``
      flattened from the API's ``{"NegativeKeywords": {"Items":
      [...]}}`` envelope via a ``model_validator(mode="before")``
      that only fires when the envelope key is explicitly in the
      input (direct construction left untouched).
      ``DirectService.get_campaigns`` opts the field into
      ``FieldNames`` for every caller. ``_build_account_budget_snapshot``
      bypasses the agent-facing ``CampaignSummary`` flattener and
      reads ``Campaign`` objects directly from
      ``DirectService.get_campaigns`` — defence-in-depth privacy
      split: operator-configured negatives carry commercial intent
      (competitor names / brand misspells / regulated-product
      filters) and never reach the agent's ``list_campaigns`` tool
      response or the CLI ``--json`` output. Pinned with a
      ``hasattr`` regression test on ``CampaignSummary``. All three
      campaign context builders (pause / resume / set_daily_budget)
      now stamp ``ReviewContext.baseline_timestamp`` (auditor HIGH-2;
      parity with the bid context builder); ``_PRIVATE_DETAIL_KEYS``
      in ``agent/tools.py`` extended with ``"missing"`` to mirror
      the audit-sink redaction so KS#3 ``CheckResult.details["missing"]``
      never reaches the LLM (auditor HIGH-1). Net effect: KS#3
      blocks resume only when a campaign actually lacks a required
      phrase, and proceeds to the confirm path when compliant —
      previously KS#3 would have blocked unconditionally on the
      first operator who configured the floor. 11 new tests
      (4 model + 2 client + 5 service + 1 tool); 563 total green.
- [x] **M2 follow-up — Per-keyword `AccountBidSnapshot` reader
      for KS#2 / KS#4** — closes the gap that left both kill-
      switches deferring on every bid call. ``Keyword`` model gains
      ``CampaignId``, ``Bid``, ``ContextBid`` and a ``Productivity``
      envelope, and exposes ``current_search_bid_rub`` /
      ``current_network_bid_rub`` / ``quality_score`` via computed
      properties (micro-RUB → RUB at the boundary; rounded int
      0..10 from ``Productivity.Value`` with out-of-range values
      falling back to ``None`` so KS#4's "QS=None → defer" branch
      stays in charge of the unexpected-input case).
      ``DirectService.get_keywords`` accepts a keyword-only
      ``keyword_ids`` selection (so the bid-context builder fetches
      by keyword id rather than running a second adgroup-lookup
      round trip), broadens ``FieldNames`` to include the new
      fields for every caller, and refuses calls with no selection
      at all. ``BiddingService._build_bid_context`` issues exactly
      one ``get_keywords(keyword_ids=...)`` call and populates a
      ``KeywordSnapshot`` per row that survives the identity
      check (``Id`` and ``CampaignId`` both present). Net result:
      a bid above ``Policy.max_cpc.campaign_max_cpc_rub`` raises
      ``PlanRejected`` at plan-creation time (KS#2); a bid
      INCREASE on a keyword whose Productivity-derived QS is
      below ``min_quality_score_for_bid_increase`` raises the
      same (KS#4); a DECREASE on a low-QS keyword still passes.
      Tightening max-CPC / min-QS thresholds in
      ``agent_policy.yml`` is now meaningful. 25 new tests
      (13 model + 5 client + 7 service); 542 total green.
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
      ``docs/OPERATING.md`` (then ``CLAUDE_DESKTOP.md``) shipped with copy-pasteable
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
