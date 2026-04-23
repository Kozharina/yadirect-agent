"""Error hierarchy.

Design: narrow exception types so callers can decide what to retry, what to
surface to the user, and what to escalate to the agent for reasoning.

- YaDirectError         base for everything from Direct
- AuthError             401/403 — stop and fix credentials
- RateLimitError        429 / Units depleted — back off
- ValidationError       400 with request-level errors — our bug, don't retry
- ApiTransientError     5xx / network — retry with backoff
- QuotaExceededError    daily points exhausted — pause agent
"""

from __future__ import annotations


class YaDirectError(Exception):
    """Base for all Yandex Direct errors."""

    def __init__(
        self,
        message: str,
        *,
        code: int | None = None,
        request_id: str | None = None,
        detail: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.request_id = request_id
        self.detail = detail

    def __str__(self) -> str:
        parts = [super().__str__()]
        if self.code is not None:
            parts.append(f"code={self.code}")
        if self.request_id:
            parts.append(f"request_id={self.request_id}")
        return " | ".join(parts)


class AuthError(YaDirectError):
    """401/403 — token invalid or lacks scope."""


class ValidationError(YaDirectError):
    """Request-level validation failure — don't retry, fix the input."""


class RateLimitError(YaDirectError):
    """Rate limit hit. Callers should back off."""


class QuotaExceededError(YaDirectError):
    """Daily points quota exhausted. Pause until midnight MSK."""


class ApiTransientError(YaDirectError):
    """Transient server/network error — safe to retry."""


class AgentSafetyError(Exception):
    """Raised by the safety layer when an operation violates policy."""


class ConfirmationRequired(Exception):  # noqa: N818 -- control-flow signal, not an error
    """Signals that an operation needs human confirmation before execution."""

    def __init__(self, plan: dict[str, object]) -> None:
        super().__init__(f"Confirmation required for: {plan.get('action')}")
        self.plan = plan
