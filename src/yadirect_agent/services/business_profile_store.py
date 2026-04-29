"""``BusinessProfileStore`` — single-JSON-file store for the operator profile (M15.4).

Sibling to ``audit.jsonl`` and ``rationale.jsonl`` under the
operator's audit dir. Holds a *single* canonical
``BusinessProfile``; ``save`` overwrites in place atomically.

Why a single JSON file rather than JSONL append-only:

- The historical-changes use case has no consumer in the code
  today. The hypothetical "I want to know when the budget
  changed from 50k to 100k" question is already answered by
  ``git`` of the file (when committed) or by the audit log
  (every save emits an event in slice 2's caller).
- Slice 3 (policy proposal) reads the *current* profile, not
  history. JSONL would add a ``latest()`` collapse layer for
  no benefit.
- One file is easier for the operator to inspect / hand-edit /
  delete than a JSONL with many revisions.

Atomicity contract: ``save`` writes to a sibling tempfile and
calls ``os.replace`` to swap it into place. POSIX guarantees
``replace`` is atomic; Windows ``os.replace`` is also atomic
since Python 3.3 (replaces ``rename`` for cross-platform use).
A partial-write crash leaves either the original file (if any)
or no file at all — never a half-written one.

Defensive ``load``: missing file, corrupt JSON, and
schema-invalid JSON all collapse to ``None``. Same shape as
``KeyringTokenStore``: all three resolve via the same operator
action (re-run onboarding), and surfacing them as three
exceptions would force every caller to handle the same recovery
three ways.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import structlog
from pydantic import ValidationError

from ..models.business_profile import BusinessProfile

_log = structlog.get_logger(component="services.business_profile_store")


class BusinessProfileStore:
    """Atomic single-JSON-file store for ``BusinessProfile``."""

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    # -- Writes ----------------------------------------------------------

    def save(self, profile: BusinessProfile) -> None:
        """Atomically replace the file with ``profile``'s JSON.

        Writes to a sibling tempfile in the same directory (so
        ``os.replace`` is a same-filesystem atomic rename) and
        renames into place. The parent directory is created on
        demand — onboarding runs before the operator has touched
        anything else, so ``logs/`` may not exist yet.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # ``delete=False`` because we close-then-rename. ``dir=parent``
        # keeps the tempfile on the same filesystem as the target,
        # which is the precondition for ``os.replace``'s atomicity.
        # ``prefix=".`` keeps the tempfile hidden in case a
        # concurrent listing happens between create and replace.
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=self._path.parent,
            prefix=f".{self._path.name}.",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            tmp.write(profile.model_dump_json())
            tmp_path = Path(tmp.name)
        try:
            os.replace(tmp_path, self._path)
        except OSError:
            # If the rename fails (rare — usually a permissions issue
            # or a target locked by another process), clean up the
            # tempfile so we don't leak state. Re-raise: the caller
            # needs to know the save did not happen.
            tmp_path.unlink(missing_ok=True)
            raise

    def delete(self) -> None:
        """Remove the file if it exists; no-op otherwise (idempotent)."""
        try:
            self._path.unlink()
        except FileNotFoundError:
            # Same shape as ``KeyringTokenStore.delete``: deleting
            # a non-existent slot is the no-op path, not an error.
            return

    # -- Reads -----------------------------------------------------------

    def load(self) -> BusinessProfile | None:
        """Return the stored profile, or ``None`` if absent / unusable.

        ``None`` covers three cases collapsed into one return
        path: missing file, corrupt JSON, and schema-invalid
        JSON (e.g. a future v2 with extra fields, or an
        operator hand-edit that dropped a required field). All
        three map to the same operator action — re-run
        onboarding — so distinguishing them at the call site
        would just route the same recovery three ways.
        """
        if not self._path.exists():
            return None
        try:
            raw = self._path.read_text(encoding="utf-8")
        except OSError:
            _log.warning("business_profile.read_failed", path=str(self._path))
            return None
        try:
            return BusinessProfile.model_validate_json(raw)
        except (json.JSONDecodeError, ValidationError):
            _log.warning(
                "business_profile.invalid_payload",
                path=str(self._path),
            )
            return None


__all__ = ["BusinessProfileStore"]
