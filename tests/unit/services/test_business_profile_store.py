"""Tests for ``BusinessProfileStore`` (M15.4 slice 2).

Pin the persistence contract for the slice 2 onboarding profile:

- ``save`` round-trips losslessly through ``load``.
- ``load`` collapses missing-file / corrupt-JSON /
  schema-invalid into a single ``None`` return — the same shape
  ``KeyringTokenStore.load`` uses, because all three resolve via
  the same operator action ("re-run onboarding").
- ``save`` is atomic via ``os.replace`` — a partial-write crash
  can never leave a half-written JSON behind.
- ``save`` creates the parent directory on demand (a fresh
  install has no ``logs/`` dir yet).
- ``delete`` is idempotent.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from yadirect_agent.models.business_profile import BusinessProfile
from yadirect_agent.services.business_profile_store import BusinessProfileStore


def _profile() -> BusinessProfile:
    return BusinessProfile(
        niche="Online courses on woodworking",
        monthly_budget_rub=50_000,
        target_cpa_rub=1_500,
    )


class TestBusinessProfileStore:
    def test_load_missing_file_returns_none(self, tmp_path: Path) -> None:
        # Fresh deployment — no ``business_profile.json`` yet.
        # The handler treats this as "no profile, ask for one".
        store = BusinessProfileStore(tmp_path / "business_profile.json")
        assert store.load() is None

    def test_save_and_load_round_trip(self, tmp_path: Path) -> None:
        store = BusinessProfileStore(tmp_path / "business_profile.json")
        original = _profile()

        store.save(original)
        restored = store.load()

        assert restored == original

    def test_save_creates_parent_dir(self, tmp_path: Path) -> None:
        # Onboarding happens before the operator has run anything
        # else; the ``logs/`` dir under ``audit_log_path.parent``
        # does not exist yet. Creating it on demand keeps the
        # caller from having to `mkdir -p` defensively at every
        # site.
        store = BusinessProfileStore(tmp_path / "fresh" / "business_profile.json")
        store.save(_profile())

        assert (tmp_path / "fresh" / "business_profile.json").exists()

    def test_save_overwrites_existing(self, tmp_path: Path) -> None:
        # Re-running onboarding (slice 2 ``profile_exists`` branch)
        # must replace the file atomically. No append, no merge.
        path = tmp_path / "business_profile.json"
        store = BusinessProfileStore(path)
        store.save(_profile())

        updated = BusinessProfile(
            niche="Plumbing services",
            monthly_budget_rub=120_000,
            target_cpa_rub=2_000,
        )
        store.save(updated)

        assert store.load() == updated

    def test_save_atomic_via_os_replace(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Atomicity contract: save writes to a sibling tempfile
        # and finalises with ``os.replace``. A partial write
        # crash leaves the original file (or no file) intact —
        # never a half-written JSON.
        import os

        replace_calls: list[tuple[str, str]] = []
        original_replace = os.replace

        def spy_replace(src: str | Path, dst: str | Path) -> None:
            replace_calls.append((str(src), str(dst)))
            original_replace(src, dst)

        monkeypatch.setattr("os.replace", spy_replace)

        path = tmp_path / "business_profile.json"
        BusinessProfileStore(path).save(_profile())

        # Pin: exactly one replace into the final path.
        assert len(replace_calls) == 1
        src, dst = replace_calls[0]
        assert dst == str(path)
        # Pin: source was a sibling tempfile, not the final path
        # itself (writing-then-renaming-into-itself would be a
        # contradiction).
        assert src != str(path)
        assert Path(src).parent == path.parent

    def test_save_does_not_leave_tempfile(self, tmp_path: Path) -> None:
        # After a successful save, only the canonical file should
        # be on disk — no ``.tmp`` siblings, no leaked temp dirs.
        path = tmp_path / "business_profile.json"
        BusinessProfileStore(path).save(_profile())

        siblings = list(path.parent.iterdir())
        assert siblings == [path]

    def test_load_returns_none_on_corrupt_json(self, tmp_path: Path) -> None:
        # Hand-edit / partial-write / unrelated file at the same
        # path → load collapses to None (same recovery as missing).
        path = tmp_path / "business_profile.json"
        path.write_text("this is not json")

        assert BusinessProfileStore(path).load() is None

    def test_load_returns_none_on_schema_violation(self, tmp_path: Path) -> None:
        # JSON parses, but ``BusinessProfile.model_validate``
        # rejects it — e.g. an older schema or a hand-edit dropped
        # ``niche``. Same None recovery: operator re-runs onboarding.
        path = tmp_path / "business_profile.json"
        path.write_text(json.dumps({"monthly_budget_rub": 50_000}))

        assert BusinessProfileStore(path).load() is None

    def test_load_returns_none_when_extra_field_present(self, tmp_path: Path) -> None:
        # Forward-compat scenario: a future ``BusinessProfile`` v2
        # adds ``icp``, an operator with v1 in keychain triggers
        # the load. ``extra="forbid"`` on the model rejects it —
        # we surface that as None so the operator re-onboards
        # rather than facing an opaque exception.
        path = tmp_path / "business_profile.json"
        path.write_text(
            json.dumps(
                {
                    "niche": "ok",
                    "monthly_budget_rub": 50_000,
                    "icp": "not yet a field",
                },
            ),
        )

        assert BusinessProfileStore(path).load() is None

    def test_delete_missing_is_idempotent(self, tmp_path: Path) -> None:
        # ``delete`` on a fresh deployment must not raise — same
        # contract as ``KeyringTokenStore.delete``. ``yadirect-agent
        # auth logout`` exits zero on the no-op path; onboarding
        # cleanup follows the same shape.
        BusinessProfileStore(tmp_path / "business_profile.json").delete()

    def test_delete_removes_file(self, tmp_path: Path) -> None:
        path = tmp_path / "business_profile.json"
        store = BusinessProfileStore(path)
        store.save(_profile())
        assert path.exists()

        store.delete()
        assert not path.exists()

    def test_path_property_exposes_constructor_value(self, tmp_path: Path) -> None:
        # Operators / tests need to know where the file lives
        # (e.g. to pre-populate it in fixtures, or to delete it
        # in CI cleanup). Mirror ``RationaleStore.path``.
        target = tmp_path / "business_profile.json"
        store = BusinessProfileStore(target)
        assert store.path == target
