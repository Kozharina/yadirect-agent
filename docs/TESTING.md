# Testing strategy

> Goal: confidence that the agent won't silently misspend. We achieve it
> with a tight unit layer, an `respx`-driven HTTP layer, and an opt-in
> VCR layer for end-to-end sanity in sandbox. No unit test ever touches
> the network.

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
- Target: **80%** on `src/yadirect_agent/`.
- Hard exclusions: `cli/main.py` (thin adapter), `__init__.py` files.
- Branch coverage on: `clients/base.py` (retry logic), `agent/safety.py`
  (policy decisions). These are the places a missed branch is expensive.

Run:

```bash
make test-cov
```

HTML report at `htmlcov/index.html`.

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
