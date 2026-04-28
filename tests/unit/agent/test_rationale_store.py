"""Tests for ``RationaleStore`` (M20.2).

The store is JSONL append-only, indexed by ``decision_id``, sibling
to ``audit.jsonl`` and ``pending_plans.jsonl``. We test:

- append + get round trip;
- append is purely additive (no overwrite, file grows);
- missing file ⇒ empty reads, not an error;
- corrupt line tolerated (does not invalidate the rest);
- list_for_resource filters by campaign id;
- list_recent windows by date.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from yadirect_agent.agent.rationale_store import RationaleStore
from yadirect_agent.models.rationale import (
    Confidence,
    InputDataPoint,
    Rationale,
)


def _rationale(
    *,
    decision_id: str = "abc123",
    action: str = "campaigns.set_daily_budget",
    resource_ids: list[int] | None = None,
    summary: str = "test rationale",
    timestamp: datetime | None = None,
) -> Rationale:
    return Rationale(
        decision_id=decision_id,
        action=action,
        resource_type="campaign",
        resource_ids=resource_ids or [],
        summary=summary,
        timestamp=timestamp or datetime.now(UTC),
    )


class TestAppendAndGet:
    def test_append_then_get_round_trips(self, tmp_path: Path) -> None:
        store = RationaleStore(tmp_path / "rationale.jsonl")
        original = _rationale(
            decision_id="abc123",
            inputs=[
                InputDataPoint(
                    name="cpa",
                    value=850.0,
                    source="metrika",
                    observed_at=datetime(2026, 4, 20, 12, 0, tzinfo=UTC),
                ),
            ]
            if False
            else [],
            confidence=Confidence.HIGH,
        )

        store.append(original)
        loaded = store.get("abc123")

        assert loaded is not None
        assert loaded.decision_id == "abc123"
        assert loaded.action == original.action

    def test_get_missing_returns_none(self, tmp_path: Path) -> None:
        store = RationaleStore(tmp_path / "rationale.jsonl")

        assert store.get("nonexistent") is None

    def test_get_on_missing_file_returns_none(self, tmp_path: Path) -> None:
        # Fresh deployment with no rationale yet — must not raise.
        store = RationaleStore(tmp_path / "does_not_exist.jsonl")

        assert store.get("anything") is None

    def test_append_creates_parent_dirs(self, tmp_path: Path) -> None:
        deep_path = tmp_path / "logs" / "agent" / "rationale.jsonl"
        store = RationaleStore(deep_path)

        store.append(_rationale())

        assert deep_path.exists()


class TestAppendOnlySemantics:
    def test_two_appends_with_same_id_keep_both_lines(self, tmp_path: Path) -> None:
        # Append-only JSONL: same decision_id twice writes two lines.
        # ``get`` returns the latest; the on-disk record retains both
        # (tamper-evident — same pattern as PendingPlansStore).
        path = tmp_path / "rationale.jsonl"
        store = RationaleStore(path)
        store.append(_rationale(decision_id="x", summary="first"))
        store.append(_rationale(decision_id="x", summary="second"))

        with path.open() as f:
            lines = [line for line in f if line.strip()]
        assert len(lines) == 2

        loaded = store.get("x")
        assert loaded is not None
        assert loaded.summary == "second"

    def test_corrupt_line_tolerated(self, tmp_path: Path) -> None:
        path = tmp_path / "rationale.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        # Manually craft a file with one valid and one corrupt line.
        valid = _rationale(decision_id="ok").model_dump_json()
        with path.open("w", encoding="utf-8") as f:
            f.write("not even json\n")
            f.write(valid + "\n")
            f.write('{"decision_id": "missing-required-fields"}\n')

        store = RationaleStore(path)

        # The valid line must still surface.
        assert store.get("ok") is not None
        # The corrupt lines didn't crash anything.
        assert store.get("missing-required-fields") is None


class TestListByResource:
    def test_returns_only_rationales_touching_campaign(
        self,
        tmp_path: Path,
    ) -> None:
        store = RationaleStore(tmp_path / "rationale.jsonl")
        store.append(_rationale(decision_id="r1", resource_ids=[42]))
        store.append(_rationale(decision_id="r2", resource_ids=[51]))
        store.append(_rationale(decision_id="r3", resource_ids=[42, 99]))

        for_42 = store.list_for_resource(campaign_id=42)

        ids = sorted(r.decision_id for r in for_42)
        assert ids == ["r1", "r3"]

    def test_empty_account_returns_empty(self, tmp_path: Path) -> None:
        store = RationaleStore(tmp_path / "rationale.jsonl")

        assert store.list_for_resource(campaign_id=42) == []


class TestListRecent:
    def test_returns_only_within_window(self, tmp_path: Path) -> None:
        now = datetime.now(UTC)
        old = now - timedelta(days=10)
        recent = now - timedelta(days=2)

        store = RationaleStore(tmp_path / "rationale.jsonl")
        store.append(_rationale(decision_id="old", timestamp=old))
        store.append(_rationale(decision_id="recent", timestamp=recent))

        last_week = store.list_recent(days=7)

        ids = [r.decision_id for r in last_week]
        assert "recent" in ids
        assert "old" not in ids

    def test_zero_days_returns_empty(self, tmp_path: Path) -> None:
        # ``--days=0`` means "no window"; we treat it as empty rather
        # than "everything".
        store = RationaleStore(tmp_path / "rationale.jsonl")
        store.append(_rationale(decision_id="x", timestamp=datetime.now(UTC)))

        with pytest.raises(ValueError, match="positive"):
            store.list_recent(days=0)

    def test_results_sorted_newest_first(self, tmp_path: Path) -> None:
        now = datetime.now(UTC)
        store = RationaleStore(tmp_path / "rationale.jsonl")
        store.append(
            _rationale(decision_id="oldest", timestamp=now - timedelta(days=3)),
        )
        store.append(
            _rationale(decision_id="newest", timestamp=now - timedelta(days=1)),
        )
        store.append(
            _rationale(decision_id="middle", timestamp=now - timedelta(days=2)),
        )

        recent = store.list_recent(days=7)

        assert [r.decision_id for r in recent] == ["newest", "middle", "oldest"]
