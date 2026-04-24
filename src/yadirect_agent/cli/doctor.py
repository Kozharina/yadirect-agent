"""`yadirect-agent doctor` — environment diagnostics.

Runs four independent checks in sequence and reports a one-line status
per check. Each function here is called from `cli/main.py::doctor_cmd`;
they are kept small, pure where possible, and return a structured
`CheckResult` so the CLI layer can render them without branching.

Skeleton commit — every check returns ``fail`` with ``"not implemented"``
so the test suite trips on assertions (right reason) rather than
``ImportError`` (wrong reason). Real implementations land next.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from ..clients.direct import DirectService  # noqa: F401 - imported for test monkeypatch

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Settings


CheckStatus = Literal["ok", "fail", "warn"]


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: CheckStatus
    detail: str


async def check_env(_settings: Settings) -> CheckResult:
    """Verify that mandatory secrets / tokens are set to non-placeholder values."""
    return CheckResult(name="env", status="fail", detail="not implemented")


async def check_anthropic(_settings: Settings, *, client: Any | None = None) -> CheckResult:
    """Issue a minimal Anthropic call and report whether credentials work."""
    return CheckResult(name="anthropic", status="fail", detail="not implemented")


async def check_direct_sandbox(_settings: Settings) -> CheckResult:
    """Hit the Direct sandbox with a cheap `campaigns.get` to prove the token."""
    return CheckResult(name="direct", status="fail", detail="not implemented")


def check_policy_file(_settings: Settings) -> CheckResult:
    """Warn if `agent_policy.yml` is missing; M2 will validate schema."""
    return CheckResult(name="policy", status="fail", detail="not implemented")
