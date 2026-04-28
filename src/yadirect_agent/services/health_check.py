"""Account health check service (M15.5.1) — stub.

Implementation lands in the next commit. The class exists here so
tests can import it and the in-test monkeypatch on
``ReportingService`` has a name to bind against.
"""

from __future__ import annotations

from typing import Self

from ..config import Settings
from ..models.health import HealthReport
from ..models.metrika import DateRange
from .reporting import ReportingService


class HealthCheckService:
    """Run a battery of rules over the M6 account_overview output."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    async def run_account_check(
        self,
        *,
        date_range: DateRange,
        goal_id: int | None = None,
    ) -> HealthReport:
        msg = "M15.5.1 — implementation in next commit"
        raise NotImplementedError(msg)


# Re-export so monkeypatch in tests targets a stable name.
__all__ = ["HealthCheckService", "ReportingService"]
