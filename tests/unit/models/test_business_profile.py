"""Tests for ``BusinessProfile`` (M15.4 slice 2).

Pin the contract the rest of slice 2 + slice 3 will rely on:

- Three fields (``niche``, ``monthly_budget_rub``,
  ``target_cpa_rub``) — minimum required by slice 3 (policy
  proposal) and the existing M15.5.1 high-CPA rule. ICP /
  forbidden_phrasings are deferred to M8.
- ``frozen=True`` — operators replace the profile via
  ``BusinessProfileStore.save``, never mutate in place.
- ``extra="forbid"`` — a malformed JSON file or a future
  schema-drift cannot silently coexist with the canonical
  shape.
- Validation: niche length, monthly budget floor, target_cpa
  optional but positive when present.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from yadirect_agent.models.business_profile import BusinessProfile


class TestBusinessProfile:
    def test_valid_minimum_payload(self) -> None:
        # ``target_cpa_rub`` is optional — many bricks-and-mortar
        # SMBs don't have a CPA target, they just want leads.
        # Construction without it must succeed.
        profile = BusinessProfile(
            niche="Online courses on woodworking",
            monthly_budget_rub=50_000,
        )
        assert profile.niche == "Online courses on woodworking"
        assert profile.monthly_budget_rub == 50_000
        assert profile.target_cpa_rub is None

    def test_valid_full_payload(self) -> None:
        profile = BusinessProfile(
            niche="Plumbing services in Moscow",
            monthly_budget_rub=120_000,
            target_cpa_rub=1_500,
        )
        assert profile.target_cpa_rub == 1_500

    def test_frozen_rejects_mutation(self) -> None:
        # The store contract is "atomic save replaces the file",
        # never "mutate then save". Frozen=True pins this at the
        # type level so a future caller cannot drift.
        profile = BusinessProfile(
            niche="X",
            monthly_budget_rub=1_000,
        )
        with pytest.raises(ValidationError):
            profile.niche = "Y"  # type: ignore[misc]

    def test_extra_field_rejected(self) -> None:
        # Defence-in-depth: a corrupt JSON file or future drift
        # cannot silently coexist with the canonical shape.
        with pytest.raises(ValidationError):
            BusinessProfile.model_validate(
                {
                    "niche": "ok",
                    "monthly_budget_rub": 5000,
                    "icp": "founders aged 30-45",  # not part of slice 2
                },
            )

    @pytest.mark.parametrize("niche", ["", "x", " "])
    def test_niche_min_length(self, niche: str) -> None:
        # ``min_length=2`` after stripping rules out single-letter
        # placeholders ("x" / "?") that the LLM might submit when
        # the operator answers "I dunno". A real niche needs at
        # least 2 characters of meaningful text.
        with pytest.raises(ValidationError):
            BusinessProfile(niche=niche, monthly_budget_rub=1_000)

    def test_niche_max_length(self) -> None:
        # ``max_length=200`` keeps the field a description, not
        # an essay. A 5000-char niche is the LLM dumping the
        # whole conversation transcript — refuse it at the
        # boundary.
        with pytest.raises(ValidationError):
            BusinessProfile(niche="x" * 201, monthly_budget_rub=1_000)

    def test_monthly_budget_must_meet_floor(self) -> None:
        # Below 1000 RUB/month a Direct campaign cannot meaningfully
        # run; the policy proposal in slice 3 derives a daily cap
        # from this number, and a sub-1000 monthly produces a daily
        # cap below Direct's own minimum. Floor at 1000 here so
        # invalid configurations fail at profile time, not later.
        with pytest.raises(ValidationError):
            BusinessProfile(niche="ok", monthly_budget_rub=500)

    def test_monthly_budget_zero_rejected(self) -> None:
        # Zero is a sentinel of "operator skipped the question";
        # the LLM should not submit it. Reject explicitly.
        with pytest.raises(ValidationError):
            BusinessProfile(niche="ok", monthly_budget_rub=0)

    def test_target_cpa_must_be_positive_when_present(self) -> None:
        # ``target_cpa_rub`` is optional, but if present it must
        # be positive — zero or negative CPA is a validation
        # error, not a legitimate "no target" signal (which is
        # ``None``).
        with pytest.raises(ValidationError):
            BusinessProfile(
                niche="ok",
                monthly_budget_rub=10_000,
                target_cpa_rub=0,
            )

    def test_round_trip_via_json(self) -> None:
        # The store reads via ``model_validate_json`` and writes
        # via ``model_dump_json``. Round-trip equivalence pins
        # that no field gets dropped or coerced silently.
        original = BusinessProfile(
            niche="SaaS analytics for SMB e-commerce",
            monthly_budget_rub=80_000,
            target_cpa_rub=2_500,
        )
        restored = BusinessProfile.model_validate_json(original.model_dump_json())
        assert restored == original
