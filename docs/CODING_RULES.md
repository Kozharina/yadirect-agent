# Coding rules

> Rules for writing code **inside this repo**. Pragma: if a rule here
> conflicts with something in `docs/ARCHITECTURE.md`, architecture wins.
> If it conflicts with a non-negotiable in `CLAUDE.md`, `CLAUDE.md` wins.

## <language>
- **Python 3.11+ only.** We use `StrEnum`, `Self`, and PEP 695-adjacent
  syntax. CI runs 3.11 and 3.12.
- **`from __future__ import annotations`** at the top of every module
  that defines types. Keeps annotations as strings and avoids import cycles.
- **Type everything.** `mypy --strict` passes. No `Any` leaks across module
  boundaries; if you need `Any` internally, justify with a comment.
</language>

## <async>
- **Every I/O call is async.** `httpx.AsyncClient`, `asyncio.sleep`,
  `asyncio.gather`. No `requests`, no `time.sleep`, no blocking file I/O
  inside the main path.
- **Context managers over explicit close.** Use `async with
  DirectService(settings) as svc:`.
- **Concurrency rule.** Read-only tools / calls may be gathered in parallel
  (`asyncio.gather`). **Writes are serialised** — even when they look
  independent — to make the audit timeline reconstructible.
- **No blocking in a loop of awaits.** If you need to fan out, use
  `asyncio.gather` or `asyncio.TaskGroup`.
</async>

## <typing>
- Prefer `dataclass(frozen=True)` for internal DTOs with no validation
  needs (`CampaignSummary`, `BidUpdate`).
- Prefer pydantic models for anything that touches the wire or the config.
- Use `Protocol` for pluggable dependencies (see `WordstatProvider`) — it
  keeps implementation detail out of consumers and makes testing trivial.
- Use `typing.NewType` for opaque IDs when they're easy to mix up
  (`CampaignId = NewType("CampaignId", int)` once we have real confusion).
- **Return types are required** on every public function. Private helpers
  can skip them only when type inference is obvious.
</typing>

## <errors>
- The error hierarchy in `exceptions.py` is the taxonomy. Don't add
  `RuntimeError("bad thing")`; raise the specific subclass or extend the
  hierarchy.
- **Never bare-except.** `except Exception` requires a comment explaining why
  broad catch is correct (usually: a top-level crash boundary that turns
  the exception into an audit event).
- **Re-raise, don't wrap, unless you add context.** `raise ... from exc`
  is mandatory when you wrap — never swallow the cause.
- Error messages in English (the project standard). Docstrings and
  review comments can be Russian or English, consistently within a file.
</errors>

## <logging>
- Use `structlog.get_logger().bind(component=...)`. Never the stdlib
  `logging` module directly.
- Log events as **verbs, period-separated, past-tense-neutral**:
  `campaigns.fetched`, `bids.apply.request`, `bids.apply.ok`,
  `bids.apply.failed`. The human-readable message comes from structured
  fields, not the event name.
- Attach **IDs, counts, and timings** as fields. Never concatenate into
  the message string.
- **Never log secrets.** Never log an entire API request body — shape and
  counts only. Never log an entire response body — fields you inspected.
</logging>

## <naming>
- Modules: lowercase, single word where possible (`campaigns.py`,
  `bidding.py`). Two-word module → underscored (`ab_testing.py`).
- Classes: PascalCase. Services end in `Service`, clients end in `Client` or
  `Service` (facade).
- Functions: snake_case, verbs. `list_campaigns`, not `campaigns`.
- Constants: `UPPER_SNAKE_CASE`. Private constants `_PREFIX_CONST`.
- Async functions don't get an `_async` suffix — everything is async, it's
  implied.
</naming>

## <files>
- One public class per module unless they are deeply coupled.
- Module-level docstring is mandatory for anything in `src/yadirect_agent/`:
  a line of purpose, then the "design choices" section when the module is
  load-bearing (see `clients/base.py` for the target).
- Avoid `utils.py` / `helpers.py` kitchen sinks. If a helper lives
  somewhere, there's a principled place for it.
</files>

## <imports>
Order (enforced by ruff `I`):
1. Standard library
2. Third-party
3. First-party (`yadirect_agent.*`)

Inside first-party, **relative imports** for siblings (`from .base import X`),
**absolute** for cross-package (`from yadirect_agent.config import Settings`).

**No import-time side effects.** Nothing reads env vars or opens files at
module import. `Settings()` and `configure_logging()` run from entry points.

## <config_and_secrets>
- `SecretStr` everywhere for tokens. `.get_secret_value()` is called at the
  one place where the value must be rendered into an HTTP header or an
  SDK client — **never** logged, never round-tripped through pydantic
  `.model_dump()`.
- `.env.example` has a comment for every variable; `.env` is in
  `.gitignore`.
- **Tests never use real tokens.** Fixtures construct `Settings` with
  placeholder `SecretStr` values.

## <docstrings_and_comments>
- Module docstring: **why this module exists** + design choices. Not what
  it contains (that's the module).
- Class docstring: invariants + intended use. Not the field list.
- Function docstring: one line purpose; a "Raises:" section when it raises
  anything other than validation errors.
- Comments explain **why**, not **what**. If you need to explain what, the
  code is unclear — refactor.

## <todos>
- `TODO(milestone): …` with the milestone ID. Bare `TODO` is rejected.
- `NotImplementedError` message is a sentence: *"expand_seeds not available
  via Direct API; wire up a real Wordstat provider."*
- Never ship code that would crash with an un-audited `NotImplementedError`
  when called from the agent loop.

## <performance>
- **Don't optimise what you haven't measured.** But also:
- Batch API calls where the API supports it (Direct lets you pass up to
  N objects per call in many methods — use it).
- Cache only what's expensive and stable (region IDs, goal IDs). Never
  cache bids or budgets.

## <security_and_privacy>
- **Never** retrieve the user's browsing data, system fingerprint, or
  password stores.
- Operations that modify account sharing, permissions, or billing are
  **out of scope** for the agent — the tool simply doesn't exist.
- Audit log entries are the only persistent record of a change; they must
  never contain secrets or raw API payloads.

## <the_cardinal_rule>
If you're about to spend the account's money, stop and reconsider. Would
a human agency with this task pause first? Then the bot should, too.
</the_cardinal_rule>

## <known_pitfalls>
Cross-cutting traps that bit us once and will bite again. Each entry is
two lines: the symptom and the fix.

- **Rich + typer wrap `--help` output with ANSI escapes.** Substring
  asserts on `runner.invoke(--help).output` break in CI. Read flag docs
  via `typing.get_type_hints(include_extras=True)`, not via the rendered
  help string.
- **`BidUpdate` is pydantic, not a frozen dataclass.** Frozen dataclasses
  crash `model_dump_json` for plan persistence. Stay on `BaseModel` for
  anything that flows through the plan store.
- **`pydantic.BaseModel.model_copy(update=...)` does NOT re-validate.**
  Don't rely on it to enforce field constraints. Push the constraint up
  to the source of truth — `Field(ge=...)` on the originating model is
  the right enforcement layer.
- **mypy version skew between in-venv and the pre-commit mirror.** The
  same code surfaces different error codes. When unavoidable (e.g. the
  MCP SDK's untyped decorators), confine the divergence to one file with
  a deliberate, narrow `# type: ignore[...]` comment.
- **CodeQL false positives.** It flags `...` Protocol bodies as
  `py/ineffectual-statement` and `pytest.raises` blocks as
  `py/unreachable-statement`. Both are false positives — dismiss in the
  Security tab with the standard reasoning, don't refactor around them.
- **Direct API returns HTTP 200 for logical errors.** Always inspect the
  body for an `"error"` key; never trust the status code alone. Already
  encoded in `clients/base.py`, but new client methods must respect it.
</known_pitfalls>
