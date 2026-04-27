"""Tests for the rollout-state module (M2.5).

Scope: ``RolloutState`` model validation + ``RolloutStateStore``
JSON read/write semantics. CLI ``rollout`` commands and the
Policy override are exercised in their own test files.

The rollout state-file lives next to the audit log; a fresh
deployment has no file → ``load`` returns ``None`` and the agent
falls back to the YAML's ``rollout_stage``. Once an operator runs
``yadirect-agent rollout promote --to assist``, the file is
written; subsequent agent runs read it and override the YAML.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from yadirect_agent.rollout import RolloutState, RolloutStateStore

# --------------------------------------------------------------------------
# RolloutState model.
# --------------------------------------------------------------------------


class TestRolloutStateModel:
    def test_minimal_construction(self) -> None:
        s = RolloutState(
            stage="assist",
            promoted_at=datetime(2026, 4, 27, 12, 0, tzinfo=UTC),
            promoted_by="ops@example.com",
            previous_stage="shadow",
        )
        assert s.stage == "assist"
        assert s.previous_stage == "shadow"

    def test_round_trip_through_json(self) -> None:
        s = RolloutState(
            stage="autonomy_light",
            promoted_at=datetime(2026, 4, 27, 12, 0, tzinfo=UTC),
            promoted_by="ops@example.com",
            previous_stage="assist",
        )
        revived = RolloutState.model_validate_json(s.model_dump_json())
        assert revived == s

    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            RolloutState.model_validate(
                {
                    "stage": "shadow",
                    "promoted_at": datetime.now(UTC).isoformat(),
                    "promoted_by": "x",
                    "previous_stage": "shadow",
                    "mystery": True,
                }
            )

    def test_unknown_stage_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RolloutState.model_validate(
                {
                    "stage": "halfway",  # not a valid RolloutStage
                    "promoted_at": datetime.now(UTC).isoformat(),
                    "promoted_by": "x",
                    "previous_stage": "shadow",
                }
            )

    def test_naive_datetime_rejected(self) -> None:
        # Audit-log conventions: timestamps are timezone-aware.
        with pytest.raises(ValidationError):
            RolloutState(
                stage="assist",
                promoted_at=datetime(2026, 4, 27, 12, 0),  # naive
                promoted_by="x",
                previous_stage="shadow",
            )

    def test_is_frozen(self) -> None:
        s = RolloutState(
            stage="shadow",
            promoted_at=datetime.now(UTC),
            promoted_by="x",
            previous_stage="shadow",
        )
        with pytest.raises(ValidationError):
            s.stage = "assist"  # type: ignore[misc]


# --------------------------------------------------------------------------
# RolloutStateStore.
# --------------------------------------------------------------------------


class TestRolloutStateStore:
    def test_load_returns_none_when_file_missing(self, tmp_path: Path) -> None:
        store = RolloutStateStore(tmp_path / "rollout_state.json")
        assert store.load() is None

    def test_save_then_load_round_trip(self, tmp_path: Path) -> None:
        path = tmp_path / "rollout_state.json"
        store = RolloutStateStore(path)
        s = RolloutState(
            stage="assist",
            promoted_at=datetime(2026, 4, 27, 12, 0, tzinfo=UTC),
            promoted_by="ops",
            previous_stage="shadow",
        )
        store.save(s)
        assert path.exists()
        revived = store.load()
        assert revived == s

    def test_save_creates_parent_dir(self, tmp_path: Path) -> None:
        # Fresh deployment: ``./logs/`` may not exist yet.
        path = tmp_path / "deeper" / "rollout_state.json"
        store = RolloutStateStore(path)
        store.save(
            RolloutState(
                stage="shadow",
                promoted_at=datetime.now(UTC),
                promoted_by="x",
                previous_stage="shadow",
            )
        )
        assert path.exists()

    def test_save_overwrites_previous(self, tmp_path: Path) -> None:
        # Unlike PendingPlansStore (append-only audit trail), the rollout
        # state-file is the CURRENT stage — overwrite is correct.
        # The audit JSONL records the promote events themselves; the
        # state-file is the just the latest snapshot.
        path = tmp_path / "rollout_state.json"
        store = RolloutStateStore(path)
        store.save(
            RolloutState(
                stage="shadow",
                promoted_at=datetime(2026, 4, 1, 12, 0, tzinfo=UTC),
                promoted_by="x",
                previous_stage="shadow",
            )
        )
        store.save(
            RolloutState(
                stage="assist",
                promoted_at=datetime(2026, 4, 27, 12, 0, tzinfo=UTC),
                promoted_by="ops",
                previous_stage="shadow",
            )
        )
        revived = store.load()
        assert revived is not None
        assert revived.stage == "assist"

    def test_load_corrupt_file_returns_none(self, tmp_path: Path) -> None:
        # A corrupt rollout_state.json must NOT crash the agent at boot.
        # ``None`` falls back to the YAML rollout_stage — safer than
        # halting the entire process. The corruption itself is
        # operator-visible via the WARNING that load() emits.
        path = tmp_path / "rollout_state.json"
        path.write_text("not valid json {{{")
        store = RolloutStateStore(path)
        assert store.load() is None

    def test_load_unreadable_file_returns_none(self, tmp_path: Path) -> None:
        """Auditor M2.5 MEDIUM: an OSError (chmod 000, symlink loop)
        must NOT crash the agent at boot. None falls back to YAML.
        """
        import os
        import sys

        if sys.platform == "win32":
            pytest.skip("chmod-based unreadability test is POSIX-only")

        path = tmp_path / "rollout_state.json"
        path.write_text(
            '{"stage":"shadow","promoted_at":"2026-04-27T12:00:00Z",'
            '"promoted_by":"x","previous_stage":"shadow"}'
        )
        os.chmod(path, 0)
        try:
            assert RolloutStateStore(path).load() is None
        finally:
            os.chmod(path, 0o600)

    def test_save_is_atomic_no_partial_file_on_crash(self, tmp_path: Path) -> None:
        """Auditor M2.5 MEDIUM: save uses tempfile + os.replace so a
        crash mid-rename leaves the destination unchanged.
        """
        from unittest.mock import patch

        path = tmp_path / "rollout_state.json"
        store = RolloutStateStore(path)
        original = RolloutState(
            stage="shadow",
            promoted_at=datetime(2026, 4, 1, tzinfo=UTC),
            promoted_by="seed",
            previous_stage="shadow",
        )
        store.save(original)
        original_content = path.read_text()

        new_state = RolloutState(
            stage="assist",
            promoted_at=datetime(2026, 4, 27, tzinfo=UTC),
            promoted_by="ops",
            previous_stage="shadow",
        )

        with patch("yadirect_agent.rollout.os.replace") as mock_replace:
            mock_replace.side_effect = OSError("simulated rename failure")
            with pytest.raises(OSError, match="simulated rename failure"):
                store.save(new_state)

        assert path.read_text() == original_content
        revived = store.load()
        assert revived is not None
        assert revived.stage == "shadow"


class TestPromotedByValidation:
    """Auditor M2.5 LOW-2: promoted_by bounded length + character set."""

    def test_rejects_too_long(self) -> None:
        with pytest.raises(ValidationError):
            RolloutState(
                stage="shadow",
                promoted_at=datetime.now(UTC),
                promoted_by="x" * 200,
                previous_stage="shadow",
            )

    def test_rejects_control_chars(self) -> None:
        with pytest.raises(ValidationError):
            RolloutState(
                stage="shadow",
                promoted_at=datetime.now(UTC),
                promoted_by="ops\x00\x01",
                previous_stage="shadow",
            )

    def test_rejects_shell_metacharacters(self) -> None:
        with pytest.raises(ValidationError):
            RolloutState(
                stage="shadow",
                promoted_at=datetime.now(UTC),
                promoted_by="ops; rm -rf /",
                previous_stage="shadow",
            )

    def test_accepts_email_style_identifier(self) -> None:
        s = RolloutState(
            stage="shadow",
            promoted_at=datetime.now(UTC),
            promoted_by="jane.smith@example.com",
            previous_stage="shadow",
        )
        assert s.promoted_by == "jane.smith@example.com"
