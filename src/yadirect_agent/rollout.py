"""Staged-rollout state-file (M2.5).

Persists which rollout stage the deployment is currently on. The
agent's ``SafetyPipeline`` reads ``Policy.rollout_stage`` to decide
which actions an agent run may attempt; this module lets an
operator promote (or roll back) the stage at runtime via
``yadirect-agent rollout promote --to <stage>`` without rewriting
the policy YAML by hand.

Layering:

- ``Policy.rollout_stage`` from ``agent_policy.yml`` is the default.
- If ``rollout_state.json`` exists at the configured path, its
  ``stage`` overrides the YAML at ``build_safety_pair`` time.
- The actual ``rollout promote`` command writes a new JSON snapshot
  AND emits an ``rollout_promote.requested|.ok|.failed`` audit
  event, so the JSONL audit trail records every transition with
  trace_id + actor + previous→new even though the state-file
  itself only carries the latest snapshot.

The state-file is overwrite-on-promote (single source of truth for
"current stage"); the audit JSONL is the append-only history.

Design notes:

- ``stage`` is a Literal restricted to the same four values as
  ``yadirect_agent.agent.safety.RolloutStage`` — keeps the
  contract single-sourced.
- Naive datetimes are rejected via ``AwareDatetime`` (matches the
  audit-event convention from M2.3a).
- ``load()`` returns ``None`` on missing or corrupt files. Boot-
  safe: the agent falls back to YAML rather than refusing to
  start.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Literal, get_args

import structlog
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from .agent.safety import RolloutStage as _SafetyRolloutStage

# Mirror the canonical RolloutStage Literal from safety.py and
# assert at module import that the two stay in sync. A future
# addition to one but not the other will fail loudly at boot rather
# than silently at the first ``model_copy`` that hits the missing
# value. Auditor M2.5 LOW-1.
RolloutStageLiteral = Literal["shadow", "assist", "autonomy_light", "autonomy_full"]
assert set(get_args(RolloutStageLiteral)) == set(get_args(_SafetyRolloutStage)), (
    "RolloutStageLiteral and safety.RolloutStage drifted: "
    f"{set(get_args(RolloutStageLiteral)) ^ set(get_args(_SafetyRolloutStage))}"
)

__all__ = [
    "RolloutStageLiteral",
    "RolloutState",
    "RolloutStateStore",
]


class RolloutState(BaseModel):
    """A snapshot of the deployment's current rollout stage.

    - ``stage``: the active stage. Overrides
      ``Policy.rollout_stage`` from YAML when this state-file is
      present.
    - ``promoted_at``: when the most recent ``rollout promote``
      command ran. Timezone-aware; matches AuditEvent convention.
    - ``promoted_by``: free-form operator identifier (email /
      username / "system" / "ci"). Today operator-supplied; M2.5
      CLI passes ``getpass.getuser()`` by default.
    - ``previous_stage``: what the stage was before this promotion.
      Useful for "rollback to previous" UX in future iterations
      and for audit-trail reconstruction.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    stage: RolloutStageLiteral
    promoted_at: AwareDatetime
    # Constrained to alphanum + the punctuation operators commonly use
    # in identifiers (email-style ``.@-_``) and bounded length so a
    # malformed CLI ``--actor`` cannot embed control codes / shell
    # metacharacters / huge strings into the audit JSONL or state-file.
    # Auditor M2.5 LOW-2.
    promoted_by: str = Field(
        ...,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9_.@\-]+$",
    )
    previous_stage: RolloutStageLiteral


class RolloutStateStore:
    """JSON-file persistence for ``RolloutState``.

    Single-snapshot semantics: ``save`` overwrites the file;
    ``load`` returns ``None`` if the file is missing or corrupt.
    The audit JSONL (M2.3) is the append-only history of every
    promote event; this file is just the cached "what stage are we
    on right now".
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> RolloutState | None:
        """Return the persisted state, or ``None`` on missing /
        corrupt / unreadable file.

        Boot-safe: any failure to read or parse the file returns
        ``None`` (with a structlog WARNING) rather than crashing.
        The agent falls back to the YAML's ``rollout_stage`` —
        safer than refusing to start. ``OSError`` is caught
        explicitly (auditor M2.5 MEDIUM): a ``chmod 000`` on the
        state-file or a symlink loop should not block boot.
        """
        if not self._path.exists():
            return None
        try:
            return RolloutState.model_validate_json(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError, OSError) as exc:
            structlog.get_logger(__name__).warning(
                "rollout_state_file_unreadable",
                path=str(self._path),
                error_type=type(exc).__name__,
                error=str(exc),
                note=(
                    "rollout_state.json failed to read/parse; falling "
                    "back to Policy.rollout_stage from YAML. Investigate "
                    "and re-promote if needed."
                ),
            )
            return None

    def save(self, state: RolloutState) -> None:
        """Persist ``state`` atomically (overwrite). Creates parent
        dirs on demand.

        Atomic-write contract (auditor M2.5 MEDIUM): the file is
        written to a sibling tempfile and renamed via ``os.replace``,
        which is atomic on POSIX and atomic-on-same-volume on
        Windows. A SIGKILL between ``open`` and ``close`` therefore
        leaves either the OLD file intact OR the NEW file fully
        written — never a truncated half. Without this, ``load``'s
        corrupt-file branch could silently fire after a crash mid-
        write and the operator's ``promote`` confirmation would be
        misleading.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # ``model_dump_json(indent=2)`` serialises AwareDatetime to
        # ISO-8601 with offset preserved.
        payload = state.model_dump_json(indent=2) + "\n"

        # Sibling tempfile in the same dir → ``os.replace`` is atomic
        # because source and destination share a filesystem.
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=self._path.parent,
            prefix=f".{self._path.name}.",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            tmp.write(payload)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp_path = Path(tmp.name)
        os.replace(tmp_path, self._path)


# Silence "unused import" check on datetime — kept as a re-export hint
# for callers who construct ``RolloutState(promoted_at=...)``.
_ = datetime
