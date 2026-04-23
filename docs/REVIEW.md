# Code review checklist

> The list the reviewer — human or Claude — walks through before approving
> a PR. Ordered from "cheap to check" to "expensive to check". Reviewer
> stops at the first hard-no.

## <pre_review_self_check>
Before asking for review, the author confirms **every one** of these:

- [ ] `make check` passes locally on a clean branch.
- [ ] Commit messages follow `CLAUDE.md#commit_style`.
- [ ] No file larger than necessary (no accidental binary, no unrelated
      reformat, no removed unrelated comments).
- [ ] PR description answers: what, why, how, what tests, what safety.
- [ ] Changes to `agent_policy.yml` schema are called out **in bold** in
      the PR body.
</pre_review_self_check>

## <tier_1_mechanical>
Takes ~1 minute. If any fails, send back without deeper review.

1. **CI is green** on both Python versions.
2. **No secrets.** Grep the diff for
   `token`, `secret`, `password`, `OAuth`, `Bearer ` (with a trailing
   space). Any hits not in `SecretStr(...)` or test fixture placeholders
   → reject.
3. **No generated / accidental files**: `.DS_Store`, `__pycache__/`,
   `.env`, `.vcr_cassettes/*.yaml` with real data.
4. **Conventional commits** on every commit in the PR.
5. **New modules have docstrings** (file-level and public classes).
</tier_1_mechanical>

## <tier_2_architecture>
Takes ~5 minutes. Reviewer opens files, not just the diff.

6. **Layer discipline** (`docs/ARCHITECTURE.md`):
   - No business logic added to `clients/`.
   - Services don't import from `agent/`.
   - `models/` has no I/O.
   - Adapters (`cli/`, `mcp_server/`) don't wrap tools by hand — they use
     the shared registry.
7. **Dependency direction.** A lower layer never imports from a higher one
   (adapter > agent > service > client > model/foundation).
8. **Async discipline.** Every new function performing I/O is `async`.
   No `time.sleep`, no `requests`, no sync `httpx.Client`.
9. **Error discipline.** New error paths raise specific subclasses from
   `exceptions.py`. `except Exception` appears only at documented crash
   boundaries.
10. **Logging discipline.** `structlog` via `get_logger(component=…)`.
    Event names are verbs in `.`-notation. No payload bodies in log
    fields.
</tier_2_architecture>

## <tier_3_safety>
Takes ~10 minutes. This is where bad things hide.

11. **Can this cause spend?** If yes:
    - [ ] Does it go through `plan → confirm → execute`?
    - [ ] Does the default policy (`agent_policy.yml` defaults) refuse
          this operation without human approval?
    - [ ] Is the operation reversible? If not — is the irreversibility
          called out in the PR description?
12. **Kill-switch coverage.** Map the change against the 7 kill-switches
    in `TECHNICAL_SPEC.md#M2.0`. If it touches budgets, bids, keywords,
    conversions, or queries — at least one switch must apply.
13. **Audit trail.** Every mutating operation emits
    `<event>.requested` / `<event>.ok` / `<event>.failed` audit events.
    No `print`, no bare structlog call in place of an audit event.
14. **Sandbox discipline.** New code paths default to sandbox. Any
    branch that selects the production base URL is gated on
    `Settings.yandex_use_sandbox == False` **and** has a corresponding
    test that exercises the sandbox branch.
15. **Secret handling.** Tokens only through `SecretStr`. The one place
    where `.get_secret_value()` is called — typically a header builder —
    is obvious and isolated.
</tier_3_safety>

## <tier_4_tests>
Takes ~5 minutes.

16. **TDD trail is visible.** `git log --oneline <base>..HEAD` on the PR
    branch shows at least one `test:` commit that precedes the matching
    `feat:` or `fix:` commit. The `test:` commit, when checked out in
    isolation, must cause `pytest -x` to fail (that's the whole point —
    we saw it fail before we made it pass).

    Exempt commit types (no red-before-green required): `refactor:`,
    `docs:`, `chore:`, `ci:`, `build:`, `style:`, `revert:`.

    If a PR bundles a feature into a single commit, the commit body must
    state "tests written first" and explain why the split was dropped —
    and the reviewer may still request the split if the diff is complex.
    See `docs/TESTING.md#tdd_workflow`.

17. **New behaviour has tests.** Every new public function or service
    method has at least:
    - a happy-path test,
    - one failure-path test (validation, auth, or transient — pick what's
      most likely),
    - a decision test (for service methods that branch on input).
18. **HTTP touches use `respx`.** No live network in unit or
    `tests/integration/`.
19. **Coverage did not drop** for the touched files (≥ 80%).
20. **Tests are fast and hermetic.** No `sleep` other than
    `asyncio.sleep(0)` for yielding. No reliance on current time unless
    the test also pins the clock (`freezegun` or a fake clock injected
    through `Settings`).
21. **Test names describe behaviour**, not implementation
    (`test_list_campaigns_filters_states`, not
    `test_list_campaigns_calls_get_campaigns_with_states`).
</tier_4_tests>

## <tier_5_dx_and_docs>
Takes ~3 minutes.

22. **Public surface documented.** If `README.md` or a `docs/*.md` page
    mentions the feature area, it's updated.
23. **Flags and env vars** introduced are listed in `.env.example` with
    a one-line comment explaining intent.
24. **Error messages** are actionable. `AuthError("expired")` is fine;
    `AuthError("")` is not.
25. **TODO markers** use `TODO(milestone): …` format and the milestone
    exists in `docs/TECHNICAL_SPEC.md`.
26. **Types survive mypy strict** and don't leak `Any` across module
    boundaries.
</tier_5_dx_and_docs>

## <tier_6_subjective>
Human reviewer only. Not a blocker — a discussion prompt.

27. Does the new code surface make the agent **easier to reason about**?
28. Is there a simpler design that would have shipped the same outcome?
29. Is naming precise? (`apply_bids` is more honest than `update_bids`
    if it also rejects some.)
30. Would I understand this PR in six months without the author next
    to me?
</tier_6_subjective>

## <review_outcomes>
Reviewer picks one of:

- **Approve** — ready to merge. No blocking comments.
- **Approve with nits** — merge after trivial cleanup the author can do
  without another review round.
- **Request changes** — at least one `tier 1`-`tier 4` item fails. Author
  addresses and re-requests review.
- **Block on design** — implementation is fine, but architecture,
  safety model, or API surface needs discussion before re-work. Move the
  conversation to an issue or a design doc in `docs/`.
</review_outcomes>
