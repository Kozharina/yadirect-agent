# Testing strategy

> Goal: confidence that the agent won't silently misspend. We achieve it
> with a tight unit layer, an `respx`-driven HTTP layer, and an opt-in
> VCR layer for end-to-end sanity in sandbox. No unit test ever touches
> the network.
>
> **Workflow note**: every behaviour change is introduced test-first. See
> `<tdd_workflow>` below.

## <tdd_workflow>
**TDD is the default here** — not a preference, not "where practical".
This is what that actually means in practice.

### The loop

1. **Red.** Write the smallest possible failing test for the next
   behaviour. Run it. It must fail — and fail *for the right reason*
   (the behaviour isn't implemented), not because of a typo, a missing
   fixture, or an import error. Fix those and re-run until the failure
   is clean.

   Commit it as its own commit:
   ```
   test(<scope>): add failing test for <behaviour>
   ```
   The commit body states what's being tested and *why it fails right
   now* (the absent function, the unenforced constraint, the missing
   branch). This is the audit trail that proves the test was honest.

2. **Green.** Write the minimum code that makes the new test pass. No
   extra features. No "while I'm here" improvements. Run the whole
   suite — no other test may regress.

   Commit:
   ```
   feat(<scope>): implement <behaviour> (test passes)
   ```
   or `fix(<scope>): ...` when addressing a bug.

3. **Refactor.** With the suite green, tidy the implementation. Extract
   helpers, rename for clarity, remove duplication. Run the full suite
   after each edit. If anything turns red, revert the last change.

   Commit (optional, only when the change is meaningful):
   ```
   refactor(<scope>): <what, briefly>
   ```

4. **Repeat** for the next small behaviour.

### What counts as a behaviour change

- A new public function / method / class / CLI flag.
- A new branch in existing code (new error type, new edge case).
- A change in an existing contract (different return shape, different
  validation rule). The old tests that no longer describe the contract
  are edited *first*, commit as `test:`, see them fail against the old
  code, then update the implementation.

Not a behaviour change (TDD exempt):
- Pure rename, pure reformatting, pure type-annotation adjustments.
- Documentation-only edits.
- Dependency bumps without API surface change.
- CI / tooling / build-script changes.
- Deleting dead code that has no tests pointing at it.

### Anti-patterns and how to spot them

| Symptom                                              | What actually happened                           |
| ---------------------------------------------------- | ------------------------------------------------ |
| `test:` and `feat:` in one commit                    | Tests written after. If impl was first, rewrite. |
| `feat:` with no `test:` anywhere in the PR           | No TDD. Back to step 1.                          |
| `test:` commit contains a test that *passes*         | Test was added after; rewrite it against the old |
|                                                      | code until it fails, commit, then fix.           |
| Pre-commit skipped on the `test:` commit             | Probably because tests failed — expected in red. |
|                                                      | Use `git commit --no-verify` only on `test:`     |
|                                                      | commits; note "-- red" in the commit subject.    |

### Red-commit convention

Because pre-commit runs `pytest` isn't one of our hooks (it runs lint +
type), most `test:` red commits pass hooks. But if a failing test
somehow breaks lint or mypy (bad import, etc.), fix that first — a red
test is *only* red because the implementation is missing, never because
the test file itself is malformed.

### Worked example

We want `CampaignService.set_daily_budget` to reject < 300 RUB.

```
$ pytest -x tests/unit/services/test_campaigns.py -k rejects
# FAIL: no attribute set_daily_budget           ← step 1, red
$ git commit -m "test(services): failing case for <300 RUB rejection"
# Edit services/campaigns.py, add the method + early raise
$ pytest -x
# PASS                                          ← step 2, green
$ git commit -m "feat(services): reject budgets below Direct's 300 RUB floor"
```

Two commits, visible red-before-green pair. A reviewer reading
`git log --oneline` knows immediately that the test was not a retrofit.

## <layers>
| Suite                      | What it covers                                | How it's mocked                          | Speed target |
| -------------------------- | --------------------------------------------- | ---------------------------------------- | ------------ |
| `tests/unit/`              | services, models, config, small pure helpers  | `monkeypatch` + in-memory fakes          | < 2 s total  |
| `tests/unit/clients/`      | HTTP clients — retries, error mapping, Units  | `respx` over `httpx.AsyncClient`         | < 3 s total  |
| `tests/integration/`       | adapter wiring (CLI, MCP) without a real API  | stub Anthropic SDK, respx for Yandex     | < 10 s total |
| `tests/sandbox/` (opt-in)  | live Yandex sandbox, marked `@pytest.mark.sandbox` | **no mock — real sandbox tokens**        | minutes      |
</layers>

## <conventions>
- **File layout mirrors `src/`.** `src/yadirect_agent/clients/base.py`
  → `tests/unit/clients/test_base.py`.
- **One behaviour per test.** Name: `test_<subject>_<behaviour>`. If you
  need `and` in the name, it's two tests.
- **Arrange / Act / Assert blocks**, separated by blank lines. No magic
  setup in class-level attributes.
- **`pytest-asyncio` in auto mode** (`asyncio_mode = "auto"`) — every
  `async def test_*` just works, no decorators.
- **Fixtures in `conftest.py`** at the tightest scope possible.
- **Parametrize error cases**. `pytest.mark.parametrize` for mapping
  many error codes to many exception types.
</conventions>

## <fixtures>
Core fixtures live in `tests/unit/conftest.py`:

- **`settings`** — a `Settings` instance with safe placeholder tokens
  (`SecretStr("test-direct-token")`) and `yandex_use_sandbox=True`. Use
  this for any code that needs config.
- **`respx_mock`** — provided by the `respx` package. Auto-reset between
  tests.
- **`anyio_backend`** — pinned to asyncio so tests are deterministic.

Service-level tests usually add a fixture that yields a `DirectService`
fake via monkeypatch:

```python
@pytest.fixture
def fake_direct_service(monkeypatch):
    """Replaces clients.direct.DirectService with an in-memory stub."""
    ...
```

## <http_layer_tests>
**Always** use `respx`. Pattern:

```python
async def test_direct_call_parses_units(settings, respx_mock):
    respx_mock.post(
        "https://api-sandbox.direct.yandex.com/json/v5/campaigns"
    ).mock(
        return_value=httpx.Response(
            200,
            headers={"Units": "10/23750/24000"},
            json={"result": {"Campaigns": []}},
        )
    )

    async with DirectApiClient(settings) as api:
        await api.call("campaigns", "get", {"SelectionCriteria": {}})
        assert api.last_units is not None
        assert api.last_units.remaining == 23750
```

**Must cover** for every new HTTP method:
- Happy path (200 with `result`)
- One retry-then-succeed path (500 → 200, 429 → 200, timeout → 200)
- One retry-exhausted path
- One validation error (200 with `{"error": …}` that maps to
  `ValidationError`)
- Auth error (maps to `AuthError`)
- Response header parsing where applicable (Units, Retry-After)

## <service_layer_tests>
Use `monkeypatch` on the client class — you want to test the service's
**decisions**, not the wire. Example:

```python
async def test_list_active_filters_on_and_suspended(settings, monkeypatch):
    captured = {}

    async def fake_get_campaigns(self, **kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(DirectService, "get_campaigns", fake_get_campaigns)

    await CampaignService(settings).list_active()

    assert set(captured["states"]) == {"ON", "SUSPENDED"}
```

## <agent_layer_tests>
Mock the Anthropic SDK response shape — the agent loop needs to be tested
without spending API credits. Stub `anthropic.messages.create` to return
a pre-scripted sequence of `tool_use` → `tool_result` → `end_turn`.

Use `agent_loop_fixture(scripted_turns=[...])` (to be built in M1).

## <sandbox_tests>
Marked with `@pytest.mark.sandbox`. Require real env vars
(`YANDEX_DIRECT_TOKEN`, `YANDEX_METRIKA_TOKEN`) and a pre-created sandbox
account. CI skips them unless `YANDEX_SANDBOX_ENABLED=1` is set.

These tests **exist to break before prod**. They check:
- OAuth token actually authenticates
- A real `campaigns.get` returns our expected shape
- A no-op `campaigns.suspend` on an already-suspended campaign doesn't
  raise (idempotency)

## <vcr>
`pytest-vcr` is available. Use sparingly — cassettes age poorly and leak
tokens if you forget to scrub. Prefer `respx` for anything that can be
expressed inline.

If you do use VCR:
- Cassettes live in `.vcr_cassettes/` (gitignored until individually
  scrubbed and opted in).
- `--record-mode=none` in CI — new tests must have the cassette committed.
- Scrub `Authorization` headers and `Client-Login` before committing.

## <coverage>
- **CI gates** the merge on `--cov-fail-under=78` today; target is **80%**
  once M7.1 dotyazhka lands (`test_bidding.py`, `test_semantics.py`).
  After that the gate moves up. PRs may not lower the gate — they may
  raise it.
- **Branch coverage on** — every branch counts, not just line coverage.
  This is why `clients/base.py` (retry/error classification) and
  `agent/loop.py` (tool dispatch decisions) are over 90%: missed branches
  there are expensive.
- **Excluded from coverage** (see `[tool.coverage.run].omit` in
  `pyproject.toml`):
  - `__init__.py` files (re-exports only).
  - `cli/main.py` — thin typer adapter; covered via CLI smoke tests, the
    orchestration logic isn't branch-dense.
  - Lines marked `pragma: no cover`, `if TYPE_CHECKING:`, Protocol stubs
    (`...`), `raise NotImplementedError`.
- **Runtime controls**:
  - `pytest-randomly` shuffles tests on each run so hidden order
    dependencies surface locally before CI.
  - `pytest-timeout` kills tests that exceed 10 s (configured in
    `[tool.pytest.ini_options]`). Override per-test with
    `@pytest.mark.timeout(N)` when a retry chain legitimately needs more.

Run locally:

```bash
make test-cov       # enforces the gate, writes htmlcov/index.html
```

CI uploads `coverage.xml` as a build artefact per Python version — grab
it if you need to inspect what was missed in a red build.

## <what_not_to_test>
- **Pydantic model field presence.** Trust pydantic; don't re-test
  `model_validate`.
- **Ruff / mypy.** That's `make check`, not a test.
- **Private helpers** that are fully exercised via a public method.
- **Third-party behaviour.** `tenacity` retries, `httpx` timeouts — mock
  the boundary, don't test the library.
</what_not_to_test>

## <failure_triage>
When a test goes red:

1. Read the failure, not the code first.
2. Is it transport-level (`respx` miss, wrong URL)? → client or fixture.
3. Is it error-mapping (wrong exception type)? → `clients/base.py`
   error-code tables.
4. Is it a service decision (wrong filter, wrong batch)? → service logic.
5. Only then open the source.
</failure_triage>
