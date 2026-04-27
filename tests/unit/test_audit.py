"""Tests for the audit-sink module (M2.3a).

Scope: model validation, JSONL roundtrip, append-only semantics,
``audit_action`` context-manager success/failure paths, PII
redaction. Wiring into services / executor lands in M2.3b.

Append-only sinks are tested with a fresh tmp_path per test so the
JSONL state is isolated.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from yadirect_agent.audit import (
    AuditEvent,
    JsonlSink,
    audit_action,
    redact_for_audit,
)

# --------------------------------------------------------------------------
# Helpers.
# --------------------------------------------------------------------------


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _now() -> datetime:
    return datetime(2026, 4, 27, 12, 0, tzinfo=UTC)


# --------------------------------------------------------------------------
# AuditEvent model.
# --------------------------------------------------------------------------


class TestAuditEventModel:
    def test_minimal_construction(self) -> None:
        e = AuditEvent(
            ts=_now(),
            actor="agent",
            action="set_campaign_budget.requested",
        )
        assert e.actor == "agent"
        assert e.args == {}
        assert e.result is None
        assert e.units_spent is None
        assert e.trace_id is None
        assert e.resource is None

    def test_round_trip_through_json(self) -> None:
        e = AuditEvent(
            ts=_now(),
            actor="agent",
            action="set_campaign_budget.ok",
            trace_id="tr-abc",
            resource="campaign:42",
            args={"campaign_id": 42, "budget_rub": 800},
            result={"status": "applied", "campaign_id": 42, "budget_rub": 800},
            units_spent=12,
        )
        revived = AuditEvent.model_validate_json(e.model_dump_json())
        assert revived == e

    def test_extra_fields_forbidden(self) -> None:
        # Schema drift must fail loudly — a typo would otherwise become a
        # silently-dropped field on every event for months.
        with pytest.raises(ValidationError):
            AuditEvent.model_validate(
                {
                    "ts": _now().isoformat(),
                    "actor": "agent",
                    "action": "x",
                    "mystery_field": True,
                }
            )

    def test_actor_must_be_known_value(self) -> None:
        with pytest.raises(ValidationError):
            AuditEvent.model_validate({"ts": _now().isoformat(), "actor": "robot", "action": "x"})

    def test_is_frozen(self) -> None:
        e = AuditEvent(ts=_now(), actor="agent", action="x")
        with pytest.raises(ValidationError):
            e.actor = "human"  # type: ignore[misc]


# --------------------------------------------------------------------------
# JsonlSink writes.
# --------------------------------------------------------------------------


class TestJsonlSink:
    @pytest.mark.asyncio
    async def test_emit_appends_line(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.jsonl"
        sink = JsonlSink(path)
        await sink.emit(AuditEvent(ts=_now(), actor="agent", action="x"))
        rows = _read_jsonl(path)
        assert len(rows) == 1
        assert rows[0]["actor"] == "agent"
        assert rows[0]["action"] == "x"

    @pytest.mark.asyncio
    async def test_emit_creates_parent_dir(self, tmp_path: Path) -> None:
        # An audit log path under a non-existent dir (typical fresh
        # deployment under ./logs/) should not require manual setup.
        path = tmp_path / "deeper" / "subdir" / "audit.jsonl"
        sink = JsonlSink(path)
        await sink.emit(AuditEvent(ts=_now(), actor="system", action="boot"))
        assert path.exists()

    @pytest.mark.asyncio
    async def test_two_emits_append_two_lines(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.jsonl"
        sink = JsonlSink(path)
        await sink.emit(AuditEvent(ts=_now(), actor="agent", action="a"))
        await sink.emit(AuditEvent(ts=_now(), actor="agent", action="b"))
        rows = _read_jsonl(path)
        assert [r["action"] for r in rows] == ["a", "b"]

    @pytest.mark.asyncio
    async def test_emit_strips_private_detail_keys(self, tmp_path: Path) -> None:
        # KS#7 raw user queries (and any future PII-prone keys) must
        # never reach the on-disk audit JSONL. Sink-level redaction
        # closes the gate even if upstream forgets.
        path = tmp_path / "audit.jsonl"
        sink = JsonlSink(path)
        await sink.emit(
            AuditEvent(
                ts=_now(),
                actor="agent",
                action="set_campaign_budget.failed",
                result={
                    "status": "rejected",
                    "blocking": [
                        {
                            "status": "blocked",
                            "reason": "query_drift",
                            "details": {
                                "new_queries_sample": [
                                    "Иванов Иван телефон",
                                    "клиника на Тверской",
                                ],
                                "new_share": 0.7,
                            },
                        }
                    ],
                },
            )
        )
        rows = _read_jsonl(path)
        assert len(rows) == 1
        details = rows[0]["result"]["blocking"][0]["details"]
        assert details["new_share"] == 0.7
        assert "new_queries_sample" not in details


class TestRedactForAudit:
    def test_drops_known_private_key(self) -> None:
        # Direct unit test on the redactor — pinned independently of
        # the sink so a future re-use of the redactor surfaces.
        redacted = redact_for_audit(
            {"new_queries_sample": ["a", "b"], "new_share": 0.7},
        )
        assert "new_queries_sample" not in redacted
        assert redacted["new_share"] == 0.7

    def test_recurses_into_nested_dicts(self) -> None:
        # Audit events nest result.blocking[].details — the redactor
        # must walk in.
        redacted = redact_for_audit(
            {
                "blocking": [
                    {
                        "details": {
                            "new_queries_sample": ["x"],
                            "ratio": 0.5,
                        }
                    }
                ]
            }
        )
        details = redacted["blocking"][0]["details"]
        assert "new_queries_sample" not in details
        assert details["ratio"] == 0.5


# --------------------------------------------------------------------------
# audit_action context manager.
# --------------------------------------------------------------------------


class TestAuditActionContextManager:
    @pytest.mark.asyncio
    async def test_success_emits_requested_then_ok(self, tmp_path: Path) -> None:
        sink = JsonlSink(tmp_path / "audit.jsonl")

        async with audit_action(
            sink,
            actor="agent",
            action="set_campaign_budget",
            resource="campaign:42",
            args={"campaign_id": 42, "budget_rub": 800},
            trace_id="tr-1",
        ) as ctx:
            ctx.set_result({"status": "applied", "campaign_id": 42, "budget_rub": 800})
            ctx.set_units_spent(12)

        rows = _read_jsonl(tmp_path / "audit.jsonl")
        assert len(rows) == 2
        assert rows[0]["action"] == "set_campaign_budget.requested"
        assert rows[0]["resource"] == "campaign:42"
        assert rows[0]["args"] == {"campaign_id": 42, "budget_rub": 800}
        assert rows[0]["trace_id"] == "tr-1"
        # ``result`` and ``units_spent`` populated only on the .ok event.
        assert rows[0].get("result") is None
        assert rows[1]["action"] == "set_campaign_budget.ok"
        assert rows[1]["result"] == {
            "status": "applied",
            "campaign_id": 42,
            "budget_rub": 800,
        }
        assert rows[1]["units_spent"] == 12

    @pytest.mark.asyncio
    async def test_exception_emits_requested_then_failed(self, tmp_path: Path) -> None:
        sink = JsonlSink(tmp_path / "audit.jsonl")

        with pytest.raises(RuntimeError, match="boom"):
            async with audit_action(
                sink,
                actor="agent",
                action="set_campaign_budget",
                args={"campaign_id": 1, "budget_rub": 500},
            ):
                raise RuntimeError("boom")

        rows = _read_jsonl(tmp_path / "audit.jsonl")
        assert len(rows) == 2
        assert rows[0]["action"] == "set_campaign_budget.requested"
        assert rows[1]["action"] == "set_campaign_budget.failed"
        # Failure event surfaces exception type and message in result.
        assert rows[1]["result"]["error_type"] == "RuntimeError"
        assert rows[1]["result"]["error_message"] == "boom"

    @pytest.mark.asyncio
    async def test_failure_does_not_lose_set_result_data(self, tmp_path: Path) -> None:
        # If the caller already populated partial result (e.g. the API
        # returned but a downstream invariant raised), preserve that
        # context alongside the error metadata.
        sink = JsonlSink(tmp_path / "audit.jsonl")

        with pytest.raises(RuntimeError):
            async with audit_action(sink, actor="agent", action="set_campaign_budget") as ctx:
                ctx.set_result({"partial": "data"})
                raise RuntimeError("after partial result")

        rows = _read_jsonl(tmp_path / "audit.jsonl")
        failed = rows[1]
        assert failed["action"] == "set_campaign_budget.failed"
        assert failed["result"]["partial"] == "data"
        assert failed["result"]["error_type"] == "RuntimeError"

    @pytest.mark.asyncio
    async def test_redacts_private_keys_in_emitted_events(self, tmp_path: Path) -> None:
        sink = JsonlSink(tmp_path / "audit.jsonl")
        async with audit_action(
            sink,
            actor="agent",
            action="set_campaign_budget",
        ) as ctx:
            ctx.set_result(
                {
                    "blocking": [
                        {
                            "details": {
                                "new_queries_sample": ["pii"],
                                "ratio": 0.5,
                            }
                        }
                    ]
                }
            )
        rows = _read_jsonl(tmp_path / "audit.jsonl")
        details = rows[1]["result"]["blocking"][0]["details"]
        assert "new_queries_sample" not in details
        assert details["ratio"] == 0.5


# --------------------------------------------------------------------------
# Sink protocol — verify that a stub implementing the contract works.
# --------------------------------------------------------------------------


class TestAuditSinkProtocol:
    @pytest.mark.asyncio
    async def test_in_memory_stub_is_compatible(self) -> None:
        # Anyone can plug a custom sink (Kafka, SQS, etc.) as long as
        # they implement ``async emit(event) -> None``. This pins the
        # contract.
        captured: list[AuditEvent] = []

        class _StubSink:
            async def emit(self, event: AuditEvent) -> None:
                captured.append(event)

        async with audit_action(_StubSink(), actor="system", action="boot"):
            pass
        assert [e.action for e in captured] == ["boot.requested", "boot.ok"]
