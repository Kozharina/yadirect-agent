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
from datetime import datetime
from pathlib import Path
from typing import Literal

import structlog
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

# Mirror the canonical RolloutStage Literal from safety.py to keep
# the type contract single-sourced — importing the safety symbol
# directly would form a service → safety dependency we don't need
# here. A drift between the two would surface in the safety test
# suite (Pydantic Literal mismatch).
RolloutStageLiteral = Literal["shadow", "assist", "autonomy_light", "autonomy_full"]

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
    promoted_by: str = Field(..., min_length=1)
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
        corrupt file.

        Boot-safe: a corrupt file returns None (with a structlog
        WARNING) rather than crashing. The agent falls back to the
        YAML's ``rollout_stage`` — safer than refusing to start.
        """
        if not self._path.exists():
            return None
        try:
            return RolloutState.model_validate_json(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError):
            structlog.get_logger(__name__).warning(
                "rollout_state_file_corrupt",
                path=str(self._path),
                note=(
                    "rollout_state.json failed to parse; falling back to "
                    "Policy.rollout_stage from YAML. Investigate and "
                    "re-promote if needed."
                ),
            )
            return None

    def save(self, state: RolloutState) -> None:
        """Persist ``state`` (overwrite). Creates parent dirs on demand."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Use ``mode="json"`` so the AwareDatetime serialises to ISO-8601
        # rather than a Python-only repr.
        payload = state.model_dump_json(indent=2)
        self._path.write_text(payload + "\n", encoding="utf-8")


# Silence "unused import" check on datetime — kept as a re-export hint
# for callers who construct ``RolloutState(promoted_at=...)``.
_ = datetime
