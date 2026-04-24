"""Tests for the M2.2 data layer: OperationPlan + PendingPlansStore.

Scope: model validation, JSONL roundtrip, append-only semantics,
status-update collapse-by-id. The pipeline that produces plans and
the executor that acts on them land in the next PR — they are not
exercised here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from yadirect_agent.agent.plans import (
    OperationPlan,
    PendingPlansStore,
    generate_plan_id,
)

# --------------------------------------------------------------------------
# Helpers.
# --------------------------------------------------------------------------


def _plan(
    plan_id: str = "plan_1",
    *,
    action: str = "set_campaign_budget",
    resource_type: str = "campaign",
    resource_ids: list[int] | None = None,
    preview: str = "raise daily budget on campaign 42 from 500 RUB to 800 RUB",
    reason: str = "change exceeds auto-approval ceiling of +20%",
    status: str = "pending",
    created_at: datetime | None = None,
) -> OperationPlan:
    return OperationPlan(
        plan_id=plan_id,
        created_at=created_at or datetime.now(UTC),
        action=action,
        resource_type=resource_type,
        resource_ids=resource_ids or [42],
        args={"campaign_id": 42, "new_budget_rub": 800},
        preview=preview,
        reason=reason,
        status=status,  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------
# OperationPlan model validation.
# --------------------------------------------------------------------------


class TestOperationPlanModel:
    def test_happy_path_round_trip(self) -> None:
        p = _plan()
        dumped = p.model_dump_json()
        revived = OperationPlan.model_validate_json(dumped)
        assert revived == p

    def test_default_status_is_pending(self) -> None:
        assert _plan().status == "pending"

    def test_rejects_empty_plan_id(self) -> None:
        with pytest.raises(ValidationError):
            _plan(plan_id="")

    def test_rejects_whitespace_in_plan_id(self) -> None:
        # plan_id is used as a CLI argument, so spaces break operator
        # ergonomics and also make the JSONL harder to grep.
        with pytest.raises(ValidationError):
            _plan(plan_id="abc def")

    def test_rejects_trailing_whitespace_in_plan_id(self) -> None:
        with pytest.raises(ValidationError):
            _plan(plan_id="abc ")

    def test_rejects_empty_action(self) -> None:
        with pytest.raises(ValidationError):
            _plan(action="")

    def test_rejects_empty_preview(self) -> None:
        # The CLI shows this in `plans list`; a blank preview forces the
        # operator to open `plans show` to understand what's happening.
        with pytest.raises(ValidationError):
            _plan(preview="")

    def test_rejects_empty_reason(self) -> None:
        with pytest.raises(ValidationError):
            _plan(reason="")

    def test_rejects_invalid_status(self) -> None:
        with pytest.raises(ValidationError):
            _plan(status="halfway")

    def test_rejects_extra_fields(self) -> None:
        with pytest.raises(ValidationError):
            OperationPlan.model_validate(
                {
                    "plan_id": "p1",
                    "created_at": datetime.now(UTC).isoformat(),
                    "action": "x",
                    "resource_type": "campaign",
                    "resource_ids": [],
                    "args": {},
                    "preview": "x",
                    "reason": "x",
                    "bonus_field": True,  # should fail
                }
            )

    def test_is_frozen(self) -> None:
        p = _plan()
        with pytest.raises(ValidationError):
            p.status = "approved"  # type: ignore[misc]


class TestGeneratePlanId:
    def test_returns_url_safe_hex(self) -> None:
        pid = generate_plan_id()
        # 8 bytes hex → 16 chars.
        assert len(pid) == 16
        assert all(ch in "0123456789abcdef" for ch in pid)

    def test_two_ids_differ(self) -> None:
        # 64 bits of entropy — collision here is more likely a bug than
        # a coincidence worth worrying about.
        assert generate_plan_id() != generate_plan_id()


# --------------------------------------------------------------------------
# PendingPlansStore: append-only JSONL semantics.
# --------------------------------------------------------------------------


class TestPendingPlansStoreIO:
    def test_missing_file_has_empty_views(self, tmp_path: Path) -> None:
        store = PendingPlansStore(tmp_path / "pending_plans.jsonl")
        assert store.all_plans() == []
        assert store.list_pending() == []
        assert store.get("anything") is None

    def test_append_creates_parent_dirs(self, tmp_path: Path) -> None:
        path = tmp_path / "deeper" / "dir" / "pending_plans.jsonl"
        store = PendingPlansStore(path)
        store.append(_plan())
        assert path.exists()

    def test_roundtrips_a_single_plan(self, tmp_path: Path) -> None:
        store = PendingPlansStore(tmp_path / "pending_plans.jsonl")
        p = _plan("alpha")
        store.append(p)

        assert store.list_pending() == [p]
        assert store.get("alpha") == p

    def test_roundtrips_multiple_plans(self, tmp_path: Path) -> None:
        store = PendingPlansStore(tmp_path / "pending_plans.jsonl")
        a = _plan("alpha")
        b = _plan("bravo", action="pause_campaigns")
        c = _plan("charlie", action="resume_campaigns")
        for p in (a, b, c):
            store.append(p)

        pending_ids = {p.plan_id for p in store.list_pending()}
        assert pending_ids == {"alpha", "bravo", "charlie"}


class TestPendingPlansStoreStatusUpdates:
    def test_update_appends_a_new_row(self, tmp_path: Path) -> None:
        path = tmp_path / "pending_plans.jsonl"
        store = PendingPlansStore(path)
        store.append(_plan("alpha"))

        updated = store.update_status("alpha", "approved")

        assert updated.status == "approved"
        assert updated.status_updated_at is not None
        # JSONL has two lines — append-only; no truncation.
        assert len(path.read_text(encoding="utf-8").splitlines()) == 2

    def test_latest_wins_by_id(self, tmp_path: Path) -> None:
        store = PendingPlansStore(tmp_path / "pending_plans.jsonl")
        store.append(_plan("alpha"))
        store.update_status("alpha", "approved")
        store.update_status("alpha", "applied")

        final = store.get("alpha")
        assert final is not None
        assert final.status == "applied"
        # list_pending now excludes it.
        assert store.list_pending() == []

    def test_updating_missing_plan_raises(self, tmp_path: Path) -> None:
        store = PendingPlansStore(tmp_path / "pending_plans.jsonl")
        with pytest.raises(KeyError):
            store.update_status("does-not-exist", "approved")

    def test_rejected_plan_is_not_in_pending(self, tmp_path: Path) -> None:
        store = PendingPlansStore(tmp_path / "pending_plans.jsonl")
        store.append(_plan("alpha"))
        store.update_status("alpha", "rejected")

        assert store.list_pending() == []
        rejected = store.get("alpha")
        assert rejected is not None
        assert rejected.status == "rejected"


class TestPendingPlansStoreRobustness:
    """The JSONL file is append-only; it must survive partial rows,
    corrupt lines, and interleaved plan_ids without losing the good
    content."""

    def test_skips_blank_lines(self, tmp_path: Path) -> None:
        path = tmp_path / "pending_plans.jsonl"
        store = PendingPlansStore(path)
        store.append(_plan("alpha"))
        # Write a blank line between valid rows.
        with path.open("a", encoding="utf-8") as f:
            f.write("\n\n")
        store.append(_plan("bravo"))

        ids = {p.plan_id for p in store.list_pending()}
        assert ids == {"alpha", "bravo"}

    def test_skips_corrupt_lines(self, tmp_path: Path) -> None:
        path = tmp_path / "pending_plans.jsonl"
        store = PendingPlansStore(path)
        store.append(_plan("alpha"))
        with path.open("a", encoding="utf-8") as f:
            f.write("}{this is not json{\n")
        store.append(_plan("bravo"))

        # One corrupt line doesn't poison the rest.
        ids = {p.plan_id for p in store.list_pending()}
        assert ids == {"alpha", "bravo"}

    def test_handles_interleaved_plans(self, tmp_path: Path) -> None:
        store = PendingPlansStore(tmp_path / "pending_plans.jsonl")
        store.append(_plan("alpha"))
        store.append(_plan("bravo"))
        store.update_status("alpha", "approved")
        store.append(_plan("charlie"))
        store.update_status("bravo", "rejected")

        pending = {p.plan_id for p in store.list_pending()}
        assert pending == {"charlie"}
        all_status = {p.plan_id: p.status for p in store.all_plans()}
        assert all_status == {
            "alpha": "approved",
            "bravo": "rejected",
            "charlie": "pending",
        }
