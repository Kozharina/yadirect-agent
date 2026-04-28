"""Tests for ``Rationale`` and its supporting models (M20.1)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from yadirect_agent.models.rationale import (
    Alternative,
    Confidence,
    InputDataPoint,
    Rationale,
)


class TestConfidence:
    def test_known_levels(self) -> None:
        assert Confidence.LOW == "low"
        assert Confidence.MEDIUM == "medium"
        assert Confidence.HIGH == "high"


class TestInputDataPoint:
    def test_minimal_construction(self) -> None:
        ts = datetime(2026, 4, 20, 12, 0, tzinfo=UTC)
        d = InputDataPoint(name="ctr", value=4.2, source="metrika", observed_at=ts)

        assert d.name == "ctr"
        assert d.value == pytest.approx(4.2)
        assert d.source == "metrika"
        assert d.observed_at == ts

    def test_value_can_be_dict_or_list(self) -> None:
        # ``value`` is intentionally Any — sometimes the input is a
        # bid history (list) or a counter snapshot (dict). Pydantic
        # should accept whatever the caller passes.
        ts = datetime(2026, 4, 20, 12, 0, tzinfo=UTC)
        d = InputDataPoint(
            name="bid_history",
            value=[10.0, 11.0, 12.5],
            source="snapshot",
            observed_at=ts,
        )

        assert d.value == [10.0, 11.0, 12.5]

    def test_empty_name_rejected(self) -> None:
        with pytest.raises(ValidationError):
            InputDataPoint(
                name="",
                value=1,
                source="metrika",
                observed_at=datetime.now(UTC),
            )

    def test_extra_field_silently_ignored(self) -> None:
        # extra="ignore" (changed from "forbid" per auditor M20 LOW-5):
        # forward-compat with future agent versions adding fields. An
        # unknown field is dropped on read rather than crashing the
        # whole record. Trade-off: typos in field names no longer
        # surface at validation; we accept this for read-side resilience.
        d = InputDataPoint.model_validate(
            {
                "name": "ctr",
                "value": 4.2,
                "source": "metrika",
                "observed_at": datetime.now(UTC).isoformat(),
                "future_field_added_in_M20_slice_3": "hi",
            },
        )

        assert d.name == "ctr"
        # Unknown field dropped, not preserved (extra="ignore" semantics).
        assert d.model_extra is None or "future_field_added_in_M20_slice_3" not in (
            d.model_extra or {}
        )

    def test_non_json_serialisable_value_rejected(self) -> None:
        # auditor M20 LOW-3: a value that can't survive JSON round-trip
        # must fail at construction, not deep inside RationaleStore.append.
        from decimal import Decimal

        ts = datetime.now(UTC)

        with pytest.raises(ValidationError, match="JSON-serialisable"):
            InputDataPoint(name="x", value=Decimal("1.5"), source="s", observed_at=ts)
        with pytest.raises(ValidationError, match="JSON-serialisable"):
            InputDataPoint(name="x", value=float("nan"), source="s", observed_at=ts)
        with pytest.raises(ValidationError, match="JSON-serialisable"):
            InputDataPoint(name="x", value=float("inf"), source="s", observed_at=ts)
        with pytest.raises(ValidationError, match="JSON-serialisable"):
            InputDataPoint(name="x", value={1, 2, 3}, source="s", observed_at=ts)

    def test_name_too_long_rejected(self) -> None:
        with pytest.raises(ValidationError):
            InputDataPoint(
                name="x" * 101,
                value=1,
                source="metrika",
                observed_at=datetime.now(UTC),
            )


class TestAlternative:
    def test_minimal(self) -> None:
        a = Alternative(
            description="raise bid by 50%",
            rejected_because="exceeds max_bid_increase_pct policy ceiling of 25%",
        )

        assert a.description == "raise bid by 50%"
        assert a.rejected_because.startswith("exceeds")

    def test_empty_description_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Alternative(description="", rejected_because="reason")

    def test_empty_rejected_because_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Alternative(description="some option", rejected_because="")


class TestRationale:
    def _minimal_kwargs(self) -> dict:
        return {
            "decision_id": "abc123",
            "action": "campaigns.set_daily_budget",
            "resource_type": "campaign",
            "summary": "Lowering budget on campaign 42 because CPA crept above target.",
        }

    def test_minimal_construction_uses_defaults(self) -> None:
        r = Rationale(**self._minimal_kwargs())

        assert r.decision_id == "abc123"
        assert r.action == "campaigns.set_daily_budget"
        assert r.resource_ids == []
        assert r.inputs == []
        assert r.alternatives_considered == []
        assert r.policy_slack == {}
        assert r.confidence == Confidence.MEDIUM
        # timestamp default is "now"; just sanity-check it's recent
        assert datetime.now(UTC) - r.timestamp < timedelta(seconds=5)

    def test_full_construction(self) -> None:
        observed = datetime(2026, 4, 20, 12, 0, tzinfo=UTC)
        r = Rationale(
            **self._minimal_kwargs(),
            resource_ids=[42],
            inputs=[
                InputDataPoint(
                    name="cpa_rub_7d",
                    value=850.0,
                    source="metrika",
                    observed_at=observed,
                ),
                InputDataPoint(
                    name="target_cpa_rub",
                    value=600.0,
                    source="settings",
                    observed_at=observed,
                ),
            ],
            alternatives_considered=[
                Alternative(
                    description="raise bid by 50%",
                    rejected_because="exceeds max_bid_increase_pct policy ceiling",
                ),
            ],
            policy_slack={"max_daily_budget_change_pct": 0.05},
            confidence=Confidence.HIGH,
        )

        assert len(r.inputs) == 2
        assert r.inputs[0].name == "cpa_rub_7d"
        assert len(r.alternatives_considered) == 1
        assert r.policy_slack == {"max_daily_budget_change_pct": 0.05}
        assert r.confidence == Confidence.HIGH

    def test_empty_decision_id_rejected(self) -> None:
        kwargs = self._minimal_kwargs()
        kwargs["decision_id"] = ""

        with pytest.raises(ValidationError):
            Rationale(**kwargs)

    def test_empty_summary_rejected(self) -> None:
        kwargs = self._minimal_kwargs()
        kwargs["summary"] = ""

        with pytest.raises(ValidationError):
            Rationale(**kwargs)

    def test_summary_too_long_rejected(self) -> None:
        # Cap at 500 — anything longer is a log entry, not a summary.
        kwargs = self._minimal_kwargs()
        kwargs["summary"] = "x" * 501

        with pytest.raises(ValidationError):
            Rationale(**kwargs)

    def test_summary_at_500_chars_accepted(self) -> None:
        kwargs = self._minimal_kwargs()
        kwargs["summary"] = "x" * 500

        r = Rationale(**kwargs)

        assert len(r.summary) == 500

    def test_extra_field_silently_ignored(self) -> None:
        # extra="ignore" — see InputDataPoint test_extra_field_silently_ignored
        # for the rationale (auditor M20 LOW-5).
        kwargs = self._minimal_kwargs()
        kwargs["future_field"] = "x"

        r = Rationale(**kwargs)

        assert r.decision_id == kwargs["decision_id"]

    def test_whitespace_decision_id_rejected(self) -> None:
        # auditor M20 MEDIUM-2: docstring promised the validator,
        # this is the validator + the test that pins it. Mirrors
        # OperationPlan.plan_id._no_whitespace_in_plan_id.
        for bad_id in [
            "has spaces",
            "tab\there",
            "newline\nhere",
            " leading-space",
            "trailing-space ",
        ]:
            kwargs = self._minimal_kwargs()
            kwargs["decision_id"] = bad_id
            with pytest.raises(ValidationError, match="whitespace"):
                Rationale(**kwargs)

    def test_inputs_capped_at_50(self) -> None:
        # auditor M20 LOW-4: > 50 input data points is bug-shaped.
        ts = datetime.now(UTC)
        too_many = [
            InputDataPoint(name=f"d{i}", value=i, source="x", observed_at=ts) for i in range(51)
        ]
        kwargs = self._minimal_kwargs()
        kwargs["inputs"] = too_many

        with pytest.raises(ValidationError):
            Rationale(**kwargs)

    def test_alternatives_capped_at_50(self) -> None:
        too_many = [Alternative(description=f"opt {i}", rejected_because="x") for i in range(51)]
        kwargs = self._minimal_kwargs()
        kwargs["alternatives_considered"] = too_many

        with pytest.raises(ValidationError):
            Rationale(**kwargs)

    def test_round_trips_through_json(self) -> None:
        # JSONL store will dump and reload — round-trip must preserve
        # everything that was put in.
        observed = datetime(2026, 4, 20, 12, 0, tzinfo=UTC)
        original = Rationale(
            **self._minimal_kwargs(),
            inputs=[
                InputDataPoint(
                    name="cpa",
                    value=850.0,
                    source="metrika",
                    observed_at=observed,
                ),
            ],
            alternatives_considered=[
                Alternative(description="x", rejected_because="y"),
            ],
            policy_slack={"max_cpc": 12.5},
            confidence=Confidence.HIGH,
        )

        as_json = original.model_dump_json()
        round_tripped = Rationale.model_validate_json(as_json)

        assert round_tripped.decision_id == original.decision_id
        assert round_tripped.inputs[0].observed_at == observed
        assert round_tripped.alternatives_considered[0].description == "x"
        assert round_tripped.policy_slack == {"max_cpc": 12.5}
        assert round_tripped.confidence == Confidence.HIGH
