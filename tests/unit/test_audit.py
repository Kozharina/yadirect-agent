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


# --------------------------------------------------------------------------
# emit-failure semantics (auditor C-1).
# --------------------------------------------------------------------------


class TestAuditActionEmitFailureSemantics:
    """The audit emit MUST NOT mask the wrapped operation's outcome.

    Disk-full (or any sink-side I/O error) loses the audit record but
    never propagates to the caller — the operator must see the
    business-logic exception, not a confusing "audit_action raised
    OSError" with the original buried under __context__.
    """

    @pytest.mark.asyncio
    async def test_emit_oserror_on_ok_path_does_not_propagate(self) -> None:
        # Wrapped block succeeded → operation is done → caller MUST NOT
        # see an I/O error from the audit sink (disk full, broken pipe,
        # permission denied — anything OSError-shaped). Auditor M2.3a
        # ADVISORY-1: this guard was previously ``except Exception``,
        # masking programmer bugs too. Narrowed to OSError; programmer
        # errors are covered by the next test.

        class _BrokenOnOkSink:
            def __init__(self) -> None:
                self.calls: list[str] = []

            async def emit(self, event: AuditEvent) -> None:
                self.calls.append(event.action)
                if event.action.endswith(".ok"):
                    raise OSError("disk full on ok")

        sink = _BrokenOnOkSink()
        # No exception should escape audit_action even though .ok emit raises.
        async with audit_action(sink, actor="agent", action="set_campaign_budget") as ctx:
            ctx.set_result({"status": "applied"})

        assert sink.calls == [
            "set_campaign_budget.requested",
            "set_campaign_budget.ok",
        ]

    @pytest.mark.asyncio
    async def test_emit_programmer_error_on_ok_path_propagates(self) -> None:
        # A programmer bug in a sink subclass (e.g. ValidationError on a
        # malformed AuditEvent, TypeError from a refactor mismatch,
        # AttributeError from a missing field) must NOT be hidden behind
        # a structlog warning. The wrapped operation succeeded, so the
        # operator's API call is fine — but the audit record is broken
        # and the operator must see that immediately, not discover it
        # weeks later when reconciliation fails. Auditor M2.3a ADVISORY-1.

        class _BrokenWithTypeErrorSink:
            async def emit(self, event: AuditEvent) -> None:
                if event.action.endswith(".ok"):
                    raise TypeError("malformed event payload")

        with pytest.raises(TypeError, match="malformed event payload"):
            async with audit_action(
                _BrokenWithTypeErrorSink(), actor="agent", action="set_campaign_budget"
            ) as ctx:
                ctx.set_result({"status": "applied"})

    @pytest.mark.asyncio
    async def test_emit_failure_on_failed_path_does_not_mask_original(self) -> None:
        # Wrapped block raised RuntimeError("api failed"); the .failed
        # emit then fails too. The caller MUST see the api-failed
        # exception — not the disk-full one.

        class _BrokenOnFailedSink:
            async def emit(self, event: AuditEvent) -> None:
                if event.action.endswith(".failed"):
                    raise OSError("disk full on failed")

        with pytest.raises(RuntimeError, match="api failed"):
            async with audit_action(
                _BrokenOnFailedSink(), actor="agent", action="set_campaign_budget"
            ):
                raise RuntimeError("api failed")

    @pytest.mark.asyncio
    async def test_programmer_error_on_failed_path_does_not_mask_original(
        self,
    ) -> None:
        # Wrapped block raised RuntimeError; the .failed emit then raises
        # a programmer error (TypeError). The caller MUST still see the
        # original RuntimeError — the audit-emit failure is logged loudly
        # but never replaces the wrapped-operation exception, otherwise
        # the operator gets a confusing TypeError with the actual API
        # failure buried under ``__context__``. Auditor M2.3a
        # ADVISORY-1.

        class _BrokenWithTypeErrorOnFailed:
            async def emit(self, event: AuditEvent) -> None:
                if event.action.endswith(".failed"):
                    raise TypeError("malformed event payload")

        with pytest.raises(RuntimeError, match="api failed"):
            async with audit_action(
                _BrokenWithTypeErrorOnFailed(), actor="agent", action="set_campaign_budget"
            ):
                raise RuntimeError("api failed")

    @pytest.mark.asyncio
    async def test_cancelled_error_on_failed_path_does_not_silently_lose_original(
        self,
    ) -> None:
        # ``asyncio.CancelledError`` inherits from ``BaseException``,
        # not ``Exception`` — so the ``except OSError`` / ``except
        # Exception`` guards on the failure path do NOT catch it.
        # Plausible during async shutdown: ``JsonlSink._append`` runs
        # via ``asyncio.to_thread`` which is cancellable. Auditor
        # M2.3a-narrow second-pass HIGH.
        #
        # Contract: the ``CancelledError`` must propagate (task
        # infrastructure expects it), but the original wrapped-
        # operation exception MUST be preserved as ``__context__``
        # so the operator's debugging path isn't lost. Today's
        # implementation lets ``CancelledError`` propagate but the
        # original ``exc`` is only on the implicit chain — pin
        # both: (a) caller sees ``CancelledError``, (b)
        # ``__context__`` carries the original ``RuntimeError``.
        import asyncio

        class _CancelledOnFailedSink:
            async def emit(self, event: AuditEvent) -> None:
                if event.action.endswith(".failed"):
                    raise asyncio.CancelledError()

        # Plain ``try/except`` rather than ``pytest.raises`` — CodeQL's
        # py/unreachable-statement check trips on the post-``with`` block
        # when the body unconditionally raises (CLAUDE.md gotcha).
        caught: BaseException | None = None
        try:
            async with audit_action(
                _CancelledOnFailedSink(), actor="agent", action="set_campaign_budget"
            ):
                raise RuntimeError("api failed")
        except asyncio.CancelledError as exc:
            caught = exc

        assert caught is not None, "expected CancelledError to propagate"

        # Original wrapped exception must be reachable via the
        # exception chain — not silently dropped.
        chain_types: set[type[BaseException]] = set()
        cur: BaseException | None = caught
        while cur is not None:
            chain_types.add(type(cur))
            cur = cur.__context__
        assert RuntimeError in chain_types, f"original RuntimeError lost from chain: {chain_types}"

    @pytest.mark.asyncio
    async def test_emit_failure_on_requested_path_propagates(self) -> None:
        # The .requested event is the precondition for any audit
        # contract. If it fails, the wrapped block has not yet run —
        # propagating the exception is correct (no money was spent).

        class _BrokenOnRequestedSink:
            async def emit(self, event: AuditEvent) -> None:
                raise OSError("disk full on requested")

        wrapped_ran = False
        with pytest.raises(OSError, match="disk full on requested"):
            async with audit_action(
                _BrokenOnRequestedSink(), actor="agent", action="set_campaign_budget"
            ):
                wrapped_ran = True

        assert wrapped_ran is False


class TestAuditEventTimezoneStrict:
    def test_naive_datetime_rejected(self) -> None:
        # Auditor M-1: the audit log must be sortable / comparable
        # across timezones. Pydantic ``AwareDatetime`` rejects naive
        # values — this test pins the constraint.
        from datetime import datetime as _dt

        with pytest.raises(ValidationError):
            AuditEvent(
                ts=_dt(2026, 4, 27, 12, 0),  # no tzinfo
                actor="agent",
                action="x",
            )


class TestPrivateKeyMissingForKS3:
    def test_redact_drops_ks3_missing_list(self) -> None:
        # Auditor M-2: KS#3 (negative-keyword floor) populates
        # ``CheckResult.details["missing"]`` with operator-supplied
        # negative keyword phrases. Operators may configure brand /
        # competitor / sensitive terms; redactor must drop the key.
        redacted = redact_for_audit(
            {
                "missing": ["BrandX", "Competitor Y", "sensitive medical phrase"],
                "campaign_id": 42,
            }
        )
        assert "missing" not in redacted
        assert redacted["campaign_id"] == 42


# --------------------------------------------------------------------------
# infer_actor_from_frame — shared helper deduped from CampaignService /
# BiddingService ``_infer_actor`` methods. Both services return ``human``
# when the @requires_plan decorator's ``wrapper`` closure has
# ``_applying_plan_id`` set in its locals (apply-plan re-entry path);
# every other call returns ``agent``. The frame walk is bounded so a
# deeply-nested test or middleware doesn't wander into unrelated locals.
# --------------------------------------------------------------------------


class TestInferActorFromFrame:
    def test_returns_agent_when_no_wrapper_frame_above(self) -> None:
        """Direct call from a vanilla function — no @requires_plan
        wrapper anywhere up the stack. Default verdict is the
        agent path."""
        from yadirect_agent.audit import infer_actor_from_frame

        assert infer_actor_from_frame() == "agent"

    def test_returns_human_when_wrapper_has_applying_plan_id(self) -> None:
        """Canonical bypass shape: a frame literally named ``wrapper``
        (the closure name @requires_plan produces) carries
        ``_applying_plan_id`` as a local. Operator drove the call
        via apply-plan, so actor is ``human``."""
        from yadirect_agent.audit import infer_actor_from_frame

        def wrapper() -> str:
            _applying_plan_id = "test-plan"
            actor = infer_actor_from_frame()
            # Sentinel read so CodeQL ``py/unused-local-variable``
            # sees the local as used. The frame walker reads it
            # via ``frame.f_locals``, which is invisible to lint.
            assert _applying_plan_id == "test-plan"
            return actor

        assert wrapper() == "human"

    def test_ignores_wrapper_frame_without_applying_plan_id(self) -> None:
        """``wrapper``-named frame whose locals do NOT include
        ``_applying_plan_id`` is the agent path. The decorator's
        non-bypass branch produces exactly this shape: same
        wrapper closure, no kwarg threaded through."""
        from yadirect_agent.audit import infer_actor_from_frame

        def wrapper() -> str:
            return infer_actor_from_frame()

        assert wrapper() == "agent"

    def test_ignores_applying_plan_id_in_non_wrapper_frames(self) -> None:
        """Auditor HIGH lesson baked in: matching ``_applying_plan_id``
        in ANY frame's locals (not just the canonical decorator
        wrapper) made actor classification sensitive to local-name
        collisions in middleware / orchestration / test code.
        Pin: a non-``wrapper`` frame with that local name does
        NOT flip the verdict."""
        from yadirect_agent.audit import infer_actor_from_frame

        def some_other_function() -> str:
            _applying_plan_id = "test-plan"
            actor = infer_actor_from_frame()
            # Sentinel read — see test_returns_human_when_wrapper_has_applying_plan_id.
            assert _applying_plan_id == "test-plan"
            return actor

        assert some_other_function() == "agent"

    def test_walk_is_bounded_at_eight_frames(self) -> None:
        """A wrapper frame more than 8 stack frames above the call
        site is not considered. Bounds prevent runaway frame-walks
        in deeply-nested contexts."""
        from yadirect_agent.audit import infer_actor_from_frame

        def wrapper() -> str:
            _applying_plan_id = "test-plan"
            actor = level_1()
            # Sentinel read — see test_returns_human_when_wrapper_has_applying_plan_id.
            assert _applying_plan_id == "test-plan"
            return actor

        def level_1() -> str:
            return level_2()

        def level_2() -> str:
            return level_3()

        def level_3() -> str:
            return level_4()

        def level_4() -> str:
            return level_5()

        def level_5() -> str:
            return level_6()

        def level_6() -> str:
            return level_7()

        def level_7() -> str:
            return level_8()

        def level_8() -> str:
            # 9 frames between the wrapper and the call to
            # infer_actor_from_frame (level_1..level_8 + this one),
            # so the walk's 8-frame ceiling means the wrapper's
            # _applying_plan_id is not reached. Verdict: agent.
            return infer_actor_from_frame()

        assert wrapper() == "agent"
