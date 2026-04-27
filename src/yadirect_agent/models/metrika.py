"""Pydantic models for Yandex Metrika API responses and reporting DTOs.

The Metrika API uses snake_case in JSON (unlike Direct's PascalCase),
so models map almost 1:1 with no alias gymnastics. ``extra="allow"``
on wire-facing models so a new Metrika field doesn't break parsing.

``CampaignPerformance`` is the *internal* DTO that joins Direct cost/
click data with Metrika conversion data — services produce these,
the agent and CLI consume them. Lives here next to the Metrika models
because the conversion side dominates the join shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class MetrikaGoalType(StrEnum):
    """Goal types Metrika exposes via the Management API.

    StrEnum so unknown future types log as the raw string instead of
    crashing parsing — Metrika has been adding goal types over the years.
    """

    URL = "url"
    NUMBER = "number"
    DEPTH = "depth"
    ACTION = "action"
    PHONE = "phone"
    EMAIL = "email"
    MESSENGER = "messenger"
    FILE = "file"
    SEARCH = "search"
    SOCIAL = "social"
    PAYMENT_SYSTEM = "payment_system"


class MetrikaGoal(BaseModel):
    """One goal on a Metrika counter.

    Matches the response shape of GET /management/v1/counter/{id}/goals,
    which wraps each goal under a ``goals: [...]`` envelope handled at
    the client level.
    """

    model_config = ConfigDict(extra="allow")

    id: int
    name: str
    type: str  # raw string — see MetrikaGoalType for known values


class ReportRow(BaseModel):
    """One row from /stat/v1/data.

    The Metrika report endpoint returns rows shaped as
    ``{"dimensions": [{"name": "..."}], "metrics": [<float>, ...]}``.

    We keep dimensions as a list of dicts (Metrika's wire shape) rather
    than flattening to ``list[str]`` because some dimensions carry
    structured metadata (icon URLs for sources, region IDs, etc.).
    The reporting service knows which positions mean what — the model
    just preserves the wire shape faithfully.
    """

    model_config = ConfigDict(extra="allow")

    dimensions: list[dict[str, Any]] = Field(default_factory=list)
    metrics: list[float] = Field(default_factory=list)


@dataclass(frozen=True)
class DateRange:
    """Inclusive date range for Metrika queries.

    Frozen so a range can't be mutated after construction (makes
    accidental "I'll just shift end_date by one" bugs visible at the
    type level — you have to construct a new range).

    Validation happens at construction: end >= start; both are dates,
    not datetimes (Metrika's stat endpoint operates at day granularity).
    """

    start: date
    end: date

    def __post_init__(self) -> None:
        if self.end < self.start:
            msg = f"DateRange end ({self.end}) is before start ({self.start})"
            raise ValueError(msg)

    def to_metrika_strings(self) -> tuple[str, str]:
        """Return (date1, date2) in ISO-8601 strings Metrika accepts."""
        return self.start.isoformat(), self.end.isoformat()


@dataclass(frozen=True)
class CampaignPerformance:
    """Joined view of one Direct campaign's effectiveness over a window.

    Why a frozen dataclass instead of a pydantic model: this is an
    internal DTO produced by ``ReportingService``, never deserialised
    from a wire format. Frozen catches the "let me just bump
    cost_rub for the demo" anti-pattern at the type level.

    ``cpa_rub`` and ``cr_pct`` are explicitly Optional and computed by
    the service, not by the consumer:

    - ``cpa_rub = None`` when ``conversions == 0`` (would be div-by-zero)
      OR when ``cost_rub == 0`` (no spend yet). Consumers MUST treat
      None as "unknown / not applicable", never default it to 0 or
      infinity — that's how a "kill any campaign with CPA > 1000"
      rule-based check would silently nuke campaigns that haven't
      spent yet.
    - ``cr_pct`` similarly None when ``clicks == 0``.

    All money is in RUB; we do not carry currency at this layer
    (``Settings.yandex_use_sandbox`` ⇒ Direct sandbox always returns
    RUB; production accounts for the Russian Direct cabinet are RUB).
    Multi-currency is a future concern we'll address with explicit
    typing if/when it becomes real.
    """

    campaign_id: int
    campaign_name: str
    date_range: DateRange
    clicks: int
    cost_rub: float
    conversions: int
    cpa_rub: float | None
    cr_pct: float | None
