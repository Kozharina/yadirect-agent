"""`yadirect-agent doctor` — environment diagnostics.

Runs four independent checks in sequence and reports a one-line status
per check. Each function here is called from `cli/main.py::doctor_cmd`;
they are kept small, pure where possible, and return a structured
`CheckResult` so the CLI layer can render them without branching.

Design:
- Env: verify every mandatory SecretStr is non-empty.
- Anthropic: issue a one-token `messages.create` against the configured
  model; any failure (auth, network, rate limit) surfaces as fail.
- Direct: call `campaigns.get` in sandbox with `limit=1`; empty sandbox
  counts as success — we're probing auth, not content.
- Policy: `agent_policy.yml` presence only (schema validation is M2.1).

All four return a `CheckResult`; the command aggregates and renders
with a table and sets exit code accordingly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from ..clients.direct import DirectService

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Settings


CheckStatus = Literal["ok", "fail", "warn"]


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: CheckStatus
    detail: str


# --------------------------------------------------------------------------
# Env check — fast, local, no network.
# --------------------------------------------------------------------------


async def check_env(settings: Settings) -> CheckResult:
    """Verify mandatory secrets are set to non-empty values."""
    missing: list[str] = []
    if not settings.yandex_direct_token.get_secret_value():
        missing.append("YANDEX_DIRECT_TOKEN")
    if not settings.anthropic_api_key.get_secret_value():
        missing.append("ANTHROPIC_API_KEY")

    if missing:
        return CheckResult(
            name="env",
            status="fail",
            detail=f"empty: {', '.join(missing)}",
        )
    sandbox = "sandbox" if settings.yandex_use_sandbox else "PRODUCTION"
    return CheckResult(
        name="env",
        status="ok",
        detail=f"tokens present; target={sandbox}; model={settings.anthropic_model}",
    )


# --------------------------------------------------------------------------
# Anthropic ping — proves the API key and model are usable.
# --------------------------------------------------------------------------


async def check_anthropic(settings: Settings, *, client: Any | None = None) -> CheckResult:
    """Issue a one-token Anthropic call and classify the outcome.

    `client` is injected for tests; in production we build an
    `anthropic.AsyncAnthropic` from the current settings.
    """
    if client is None:
        import anthropic

        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key.get_secret_value())

    try:
        await client.messages.create(
            model=settings.anthropic_model,
            max_tokens=1,
            messages=[{"role": "user", "content": "ping"}],
        )
    except Exception as exc:
        return CheckResult(
            name="anthropic",
            status="fail",
            detail=f"{type(exc).__name__}: {exc}",
        )

    return CheckResult(
        name="anthropic",
        status="ok",
        detail=f"ping ok; model={settings.anthropic_model}",
    )


# --------------------------------------------------------------------------
# Direct sandbox ping — proves the OAuth token via campaigns.get.
# --------------------------------------------------------------------------


async def check_direct_sandbox(settings: Settings) -> CheckResult:
    """Hit the Direct sandbox with a cheap `campaigns.get` to prove the token.

    An empty sandbox is still a successful handshake — we're checking
    that the token authenticates, not that it has visible data.
    """
    try:
        async with DirectService(settings) as api:
            campaigns = await api.get_campaigns(limit=1)
    except Exception as exc:
        return CheckResult(
            name="direct",
            status="fail",
            detail=f"{type(exc).__name__}: {exc}",
        )

    target = "sandbox" if settings.yandex_use_sandbox else "prod"
    return CheckResult(
        name="direct",
        status="ok",
        detail=f"auth ok on {target}; visible campaigns={len(campaigns)}",
    )


# --------------------------------------------------------------------------
# Policy file check — presence only. Schema is M2.1.
# --------------------------------------------------------------------------


def check_policy_file(settings: Settings) -> CheckResult:
    path = settings.agent_policy_path
    if not path.exists():
        return CheckResult(
            name="policy",
            status="warn",
            detail=f"file missing at {path}; agent will refuse mutating ops until it exists",
        )
    return CheckResult(
        name="policy",
        status="ok",
        detail=f"present at {path}",
    )
