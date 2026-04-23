# Architecture

> Complement to `docs/BRIEF.md` and `docs/TECHNICAL_SPEC.md`. This file
> answers **"where does new code go and what can it depend on?"**. Enforced
> in review (`docs/REVIEW.md`) and by `mypy --strict`.

## Layers

Six layers. Dependencies only point **downward**. A ruff/import-linter rule
will catch violations once set up; until then, reviewers enforce by hand.

```
┌─────────────────────────────────────────────────────────────────────┐
│  cli/             (typer)          mcp_server/      (MCP SDK)       │
│                  ──┬──                   ──┬──                      │
│                    └──────────┬───────────┘                         │
│                               ▼                                     │
│  agent/      tools registry · agent loop · safety · policy · prompts│
│                               │                                     │
│                               ▼                                     │
│  services/   campaigns · bidding · semantics · ab_testing · reporting│
│                               │                                     │
│                               ▼                                     │
│  clients/    DirectApiClient · DirectService · MetrikaService       │
│              WordstatProvider (Protocol) + implementations          │
│                               │                                     │
│                               ▼                                     │
│  models/     pydantic v2 schemas, PascalCase aliases for API wire   │
│                                                                     │
│  config.py   exceptions.py   logging.py   audit.py   (foundation)   │
└─────────────────────────────────────────────────────────────────────┘
```

### <layer name="foundation">config, exceptions, logging, audit</layer>

- `config.py`: `Settings` (pydantic-settings). Loaded once, passed explicitly
  through dependency injection. **No global singletons.** Tests override by
  constructing a fresh `Settings`.
- `exceptions.py`: typed error hierarchy. Callers distinguish auth / quota /
  validation / transient / rate-limit. Services and the agent loop never
  catch `Exception` — they catch the specific subclass.
- `logging.py`: structlog config (json / console). `get_logger(component=…)`
  is the only public accessor.
- `audit.py` (M2.3): JSONL append-only sink for every mutating action.
  `AuditEvent` is immutable once written.

**Rules:**
- No imports from higher layers.
- No network calls. `config` validates values but does not contact Yandex.

### <layer name="models">`models/`</layer>

- Pydantic v2 only. `ConfigDict(populate_by_name=True)` with PascalCase
  aliases that mirror the Direct API v5 wire format.
- Enums use `StrEnum` for easy logging and JSON round-tripping.
- `extra="allow"` on wire-facing models so a new Direct field doesn't break us.
- Internal-only types (e.g. `CampaignSummary`) live next to the service that
  consumes them, **not** here — this layer is the public, external shape only.

**Rules:**
- No I/O.
- No dependency on `clients/` or anything above.

### <layer name="clients">`clients/`</layer>

- **HTTP transport only.** One method per API endpoint. Maps request /
  response JSON to/from `models/`. That's it.
- Retries, auth headers, error classification, rate-limit header parsing
  live here because they are transport-level concerns.
- `DirectApiClient` is the only thing that parses the `Units` header or
  classifies Direct error codes. Everything else asks it via typed
  exceptions.

**Forbidden in this layer:**
- Decisions about what to do in response to an error (that's `services/`).
- Building workflows out of multiple API calls (that's `services/`).
- Touching `agent_policy.yml` or the safety layer.
- Reading environment variables directly — they come through `Settings`.

### <layer name="services">`services/`</layer>

- **Where the thinking happens.** Multi-call workflows, invariants,
  cross-resource validation, and audit emission.
- Each service receives `Settings` via constructor and opens client sessions
  with `async with` inside its methods (cheap; keeps concurrency simple).
- Services **emit audit events** for every mutating operation — before and
  after (`*.requested`, `*.ok`, `*.failed`).
- Services expose **typed DTOs** (dataclasses or pydantic) for the tools
  layer, not raw `dict` shapes. This is what the agent sees.

**Rules:**
- No structured logging of raw API payloads if those payloads can contain
  user data. Log the shape + counts + IDs, not the content.
- Idempotency where feasible: `pause(ids)` on already-paused campaigns is
  a no-op, not an error.

### <layer name="agent">`agent/`</layer>

- `agent/tools.py`: typed tool registry. Tool descriptions are written
  **for the LLM** — what it does, when to use it, what it returns, what
  errors it raises.
- `agent/loop.py`: the tool-use loop over the Anthropic SDK. Parallelises
  read-only tools, serialises writes. Hard iteration cap. Repetition
  detector (same tool + same args N times in a row → stop).
- `agent/safety.py`: policy schema, kill-switches, plan → confirm → execute
  decorators.
- `agent/prompts.py`: system and scaffolding prompts. Exported as constants
  so they can be A/B tested and version-controlled.

**Rules:**
- Tools are **wrappers over services**. A tool never calls a client directly.
- The loop never performs I/O on behalf of a tool — it just dispatches.

### <layer name="adapters">`cli/` and `mcp_server/`</layer>

- Identical functional surface via different transports. **Share the tool
  registry; don't duplicate tools.** The MCP server wraps each registered
  tool in MCP-tool plumbing at startup.
- CLI-only concerns (interactive REPL, spinner, pretty-printed tables) are
  isolated in `cli/` and never leak into tools.
- MCP-only concerns (`--allow-write` flag, stdio transport, resource URIs
  if any) are isolated in `mcp_server/`.

## Cross-cutting contracts

- **Async only** in main paths. If a library is sync, wrap it at the
  boundary — never block an event loop mid-request.
- **Dependency injection, no singletons.** Tests construct `Settings`,
  pass it to the client/service, assert. No `importlib.reload`, no
  `monkeypatch` on module-level globals.
- **Errors are typed**. `except Exception` is a code smell. `except`
  a specific subclass, do something specific, re-raise unhandled.
- **Never log secrets.** `SecretStr.get_secret_value()` is called once,
  inside the header builder, and the result never enters a log line.
- **Audit is a sink, not a log.** Audit events are structured, append-only,
  and must be reconstructible into a timeline. They are not duplicate
  structlog output.

## Testing seam per layer

| Layer       | Preferred test style                                      |
| ----------- | --------------------------------------------------------- |
| foundation  | unit, no mocks                                            |
| models      | unit, property-based where shapes are subtle              |
| clients     | `respx` over httpx — real wire format, real retries       |
| services    | `monkeypatch` the client — fast, focused on logic         |
| agent       | fake tool registry + scripted `anthropic.messages` stub   |
| adapters    | end-to-end in sandbox, gated behind `@pytest.mark.sandbox`|

See `docs/TESTING.md` for the long version.
