"""``BusinessProfile`` — operator-supplied context about the business (M15.4).

Captured during onboarding (M15.4 slice 2) via the
``start_onboarding`` MCP tool. The agent uses it to:

- propose a sensible ``agent_policy.yml`` (slice 3) — the
  monthly budget seeds the daily cap;
- evaluate the existing M15.5.1 high-CPA health rule against an
  operator-meaningful target rather than a hard-coded number;
- ground future LLM-rendered explanations ("we paused the
  woodworking-courses campaign because…") in the operator's
  own words.

Three fields, deliberately. Slice 3 (policy) and the existing
M15.5.1 rule are the only consumers today; adding ICP /
forbidden_phrasings would be designing for the hypothetical
M8 (creatives) future.

Frozen because the store contract is "atomic save replaces the
file"; mutation-then-save would invite TOCTOU bugs across the
``BusinessProfileStore.save`` boundary.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator


class BusinessProfile(BaseModel):
    """Operator-supplied business context for the agent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    niche: str = Field(
        ...,
        min_length=2,
        max_length=200,
        description=(
            "What the business sells / does, in the operator's own words. "
            "2-200 chars: above 1 char to rule out 'x' / '?' placeholders, "
            "below 200 to keep the field a description rather than an essay."
        ),
    )
    monthly_budget_rub: int = Field(
        ...,
        ge=1_000,
        description=(
            "Total monthly Yandex.Direct budget in RUB. Floor at 1000 — "
            "below that the slice 3 policy proposal derives a daily cap "
            "below Direct's own minimum, which is invalid by definition."
        ),
    )
    target_cpa_rub: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Target cost-per-acquisition in RUB, when the business has one. "
            "Optional — many SMBs (services, brick-and-mortar) just want "
            "leads without an explicit CPA target. None means 'no target'; "
            "zero or negative is invalid."
        ),
    )

    @field_validator("niche")
    @classmethod
    def _niche_not_blank_after_strip(cls, v: str) -> str:
        # ``min_length=2`` runs against the raw string; a value of
        # "  " (two spaces) would pass min_length but is still a
        # blank niche. Strip and re-check so whitespace-only inputs
        # land in the same error path as empty strings.
        if not v.strip():
            msg = "niche must contain non-whitespace characters"
            raise ValueError(msg)
        return v


__all__ = ["BusinessProfile"]
