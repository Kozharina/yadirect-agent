"""Tests for HealthHistoryStore (M15.5.5 — CTR-drift rule infra).

The CTR-drift rule needs last-week's per-campaign CTR to compute
a drop. We persist HealthSnapshot rows in an append-only JSONL
sibling to ``audit.jsonl`` / ``rationale.jsonl`` / ``cost.jsonl``
— same operational shape every other persistent store in this
codebase has settled on.

Two layers covered:

1. Model — ``HealthSnapshot`` round-trips through
   ``to_jsonable`` / ``from_jsonable`` so a future writer/reader
   pair can't drift (date format change, missing field).
2. Store — append, load-latest-per-campaign, missing file, malformed
   line tolerance, atomic write, multi-snapshot collapse to newest.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from yadirect_agent.models.health_history import HealthSnapshot
from yadirect_agent.services.health_history_store import HealthHistoryStore

from yadirect_agent.models.metrika import DateRange


def _snapshot(
    *,
    campaign_id: int = 1,
    snapshot_at: datetime | None = None,
    end: date | None = None,
    clicks: int = 100,
    impressions: int = 10_000,
    ctr_pct: float | None = 1.0,
) -> HealthSnapshot:
    snap_at = snapshot_at or datetime(2026, 5, 6, 8, 0, tzinfo=UTC)
    end_date = end or date(2026, 5, 5)
    return HealthSnapshot(
        snapshot_at=snap_at,
        date_range=DateRange(start=date(2026, 4, 29), end=end_date),
        campaign_id=campaign_id,
        clicks=clicks,
        impressions=impressions,
        ctr_pct=ctr_pct,
    )


class TestHealthSnapshotModel:
    def test_construction_pins_required_fields(self) -> None:
        snap = _snapshot(campaign_id=42, clicks=120, impressions=10_000, ctr_pct=1.2)
        assert snap.campaign_id == 42
        assert snap.clicks == 120
        assert snap.impressions == 10_000
        assert snap.ctr_pct == 1.2

    def test_jsonable_round_trip_preserves_shape(self) -> None:
        # The persistence path writes ``to_jsonable`` and reads via
        # ``from_jsonable``. A mis-encoded date or a missing field
        # would silently corrupt previous-week comparisons; round-
        # tripping pins the contract.
        original = _snapshot(campaign_id=7, clicks=50, impressions=5_000, ctr_pct=1.0)

        as_dict = original.to_jsonable()
        restored = HealthSnapshot.from_jsonable(as_dict)

        assert restored == original

    def test_jsonable_dates_are_iso_strings(self) -> None:
        # Wire shape is human-greppable in the JSONL file; ISO is
        # the only sensible choice.
        snap = _snapshot()
        as_dict = snap.to_jsonable()
        assert as_dict["snapshot_at"] == "2026-05-06T08:00:00+00:00"
        assert as_dict["date_range"]["start"] == "2026-04-29"
        assert as_dict["date_range"]["end"] == "2026-05-05"

    def test_ctr_pct_may_be_none(self) -> None:
        # CTR is undefined when impressions == 0; the snapshot must
        # still serialise so the consumer can distinguish "campaign
        # had no impressions in the window" from "campaign didn't
        # appear in the window at all".
        snap = _snapshot(clicks=0, impressions=0, ctr_pct=None)
        restored = HealthSnapshot.from_jsonable(snap.to_jsonable())
        assert restored.ctr_pct is None

    def test_negative_clicks_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            HealthSnapshot(
                snapshot_at=datetime(2026, 5, 6, tzinfo=UTC),
                date_range=DateRange(start=date(2026, 4, 29), end=date(2026, 5, 5)),
                campaign_id=1,
                clicks=-1,
                impressions=10,
                ctr_pct=None,
            )

    def test_negative_impressions_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            HealthSnapshot(
                snapshot_at=datetime(2026, 5, 6, tzinfo=UTC),
                date_range=DateRange(start=date(2026, 4, 29), end=date(2026, 5, 5)),
                campaign_id=1,
                clicks=0,
                impressions=-5,
                ctr_pct=None,
            )


class TestHealthHistoryStore:
    def test_load_on_missing_file_returns_empty(self, tmp_path: Path) -> None:
        # Fresh deployment — first ``run_account_check`` call has
        # no prior history. Load must succeed and return an empty
        # mapping so callers can treat "no previous snapshot" as
        # "skip historical rules" rather than crash.
        store = HealthHistoryStore(tmp_path / "health_history.jsonl")
        assert store.load_latest_per_campaign() == {}

    def test_append_then_load_round_trips(self, tmp_path: Path) -> None:
        store = HealthHistoryStore(tmp_path / "health_history.jsonl")
        snaps = [
            _snapshot(campaign_id=1, ctr_pct=1.0),
            _snapshot(campaign_id=2, ctr_pct=2.5),
        ]
        store.append(snaps)

        latest = store.load_latest_per_campaign()
        assert set(latest) == {1, 2}
        assert latest[1].ctr_pct == 1.0
        assert latest[2].ctr_pct == 2.5

    def test_load_returns_latest_per_campaign(self, tmp_path: Path) -> None:
        # Two snapshots for the same campaign across two checks.
        # Latest-per-campaign must return the newer one — that's
        # the contract the CTR-drift rule depends on (it asks
        # "what was last week" and assumes one canonical answer).
        store = HealthHistoryStore(tmp_path / "health_history.jsonl")
        old = _snapshot(
            campaign_id=1,
            snapshot_at=datetime(2026, 4, 29, 8, 0, tzinfo=UTC),
            end=date(2026, 4, 28),
            ctr_pct=1.5,
        )
        new = _snapshot(
            campaign_id=1,
            snapshot_at=datetime(2026, 5, 6, 8, 0, tzinfo=UTC),
            end=date(2026, 5, 5),
            ctr_pct=0.8,
        )
        store.append([old])
        store.append([new])

        latest = store.load_latest_per_campaign()
        assert latest[1].ctr_pct == 0.8
        assert latest[1].snapshot_at == new.snapshot_at

    def test_append_creates_parent_dirs(self, tmp_path: Path) -> None:
        # Mirror RationaleStore behaviour: a fresh deployment may
        # not have ``audit.jsonl``'s parent dir yet, the first
        # write must mkdir on demand.
        store = HealthHistoryStore(tmp_path / "fresh" / "deep" / "health_history.jsonl")
        store.append([_snapshot()])
        assert store.path.exists()

    def test_corrupt_line_does_not_invalidate_rest(self, tmp_path: Path) -> None:
        # Defensive parsing: a partial-write crash or a manual
        # edit could leave one bad line. The loader must skip it
        # and surface the rest, mirroring RationaleStore /
        # PendingPlansStore semantics. Without this, one bad row
        # nukes a week of history.
        path = tmp_path / "health_history.jsonl"
        good = _snapshot(campaign_id=1, ctr_pct=1.0)
        path.write_text(
            json.dumps(good.to_jsonable())
            + "\n"
            + "{ this is not valid json }\n"
            + json.dumps(_snapshot(campaign_id=2, ctr_pct=2.0).to_jsonable())
            + "\n",
            encoding="utf-8",
        )

        store = HealthHistoryStore(path)
        latest = store.load_latest_per_campaign()
        assert set(latest) == {1, 2}

    def test_atomic_via_append_to_disk(self, tmp_path: Path) -> None:
        # Append-only JSONL means each call writes one or more new
        # lines and never rewrites the file. We pin the on-disk
        # line count to catch a regression that switched to
        # whole-file rewrite (which would be both slower and
        # non-atomic without a tempfile + os.replace dance).
        path = tmp_path / "health_history.jsonl"
        store = HealthHistoryStore(path)

        store.append([_snapshot(campaign_id=1)])
        store.append([_snapshot(campaign_id=2), _snapshot(campaign_id=3)])

        line_count = sum(1 for _ in path.open(encoding="utf-8"))
        assert line_count == 3

    def test_from_settings_classmethod_uses_audit_log_sibling(self, tmp_path: Path) -> None:
        # CLI / MCP wiring uses ``HealthHistoryStore.from_settings(s)``
        # so the path is computed in one place. Regression test:
        # the path must sit alongside ``audit.jsonl`` / ``rationale.jsonl``,
        # not in some other directory.
        from yadirect_agent.config import Settings

        settings = Settings(
            yandex_direct_token="dummy",
            yandex_metrika_token="dummy",
            yandex_metrika_counter_id=1,
            audit_log_path=tmp_path / "logs" / "audit.jsonl",
        )
        store = HealthHistoryStore.from_settings(settings)
        assert store.path == tmp_path / "logs" / "health_history.jsonl"
