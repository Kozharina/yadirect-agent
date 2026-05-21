"""Tests for ``NotificationDispatcher`` (M18 slice 5a).

Dispatcher is the fan-out layer between event producers
(``HealthCheckService`` findings today; M19 rollback / M20 explain
later) and the per-medium sinks (``TelegramSink`` today; Slack /
Email / Chat later). Three contracts pinned here:

1. **Fan-out.** ``send(notification)`` invokes EVERY configured sink,
   not just the first one. Operator who wires Telegram + Slack must
   get the message in both channels.
2. **Partial-failure tolerance.** If sink N raises, sinks N+1 still
   get called. The whole point of multi-channel is "Telegram is
   down → Slack delivers anyway"; a Dispatcher that propagated the
   first exception would defeat that promise. Per-sink failures
   become structlog warnings, not raised exceptions.
3. **``from_settings`` mirrors per-sink construction.** Settings →
   Dispatcher returns an instance whose sink list contains exactly
   the sinks whose own ``from_settings`` would have returned non-None.
   No configured channels ⇒ empty Dispatcher (still safe to call
   ``.send`` on — it's a no-op + log).

Why partial-failure tolerance is a Dispatcher concern and not a
TelegramSink concern: ``TelegramSink.send`` deliberately raises so
a single-sink caller (the ``notify test`` CLI command) sees the
failure clearly. The Dispatcher is the LAYER that decides "OK, one
sink failed, keep going" — pushing that decision into the sink
would force every sink to learn about the "you might be one of many"
case.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr

from yadirect_agent.models.health import Severity
from yadirect_agent.models.notification import Notification
from yadirect_agent.services.notify.dispatcher import NotificationDispatcher


def _make_notification() -> Notification:
    return Notification(
        severity=Severity.WARNING,
        title="Test title",
        body="Test body",
    )


class _RecordingSink:
    """Minimal in-memory sink that records every notification it was sent.

    Used instead of ``AsyncMock(spec=...)`` for the structural-typing
    tests below — the Dispatcher only relies on ``async send``, so a
    bare class with that method is exactly the Protocol surface.
    """

    def __init__(self) -> None:
        self.received: list[Notification] = []

    async def send(self, notification: Notification) -> None:
        self.received.append(notification)


class _FailingSink:
    """Sink that always raises. Used to pin partial-failure behaviour."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def send(self, notification: Notification) -> None:
        raise self._exc


class TestDispatcherFanOut:
    @pytest.mark.asyncio
    async def test_send_invokes_every_sink(self) -> None:
        # Two sinks, one notification → both receive it. This is the
        # core promise of the Dispatcher and the reason it exists at
        # all (a single-sink world wouldn't need a Dispatcher).
        sink_a = _RecordingSink()
        sink_b = _RecordingSink()
        dispatcher = NotificationDispatcher(sinks=[sink_a, sink_b])

        notif = _make_notification()
        await dispatcher.send(notif)

        assert sink_a.received == [notif]
        assert sink_b.received == [notif]

    @pytest.mark.asyncio
    async def test_send_with_no_sinks_is_noop(self) -> None:
        # Empty Dispatcher (operator hasn't wired any channels) must
        # not raise. ``health`` and ``run`` always call dispatcher.send
        # after their main payload; making them check is_enabled first
        # would push responsibility to every caller. No-op + log is
        # the right contract.
        dispatcher = NotificationDispatcher(sinks=[])
        await dispatcher.send(_make_notification())  # must not raise

    @pytest.mark.asyncio
    async def test_send_passes_through_notification_unchanged(self) -> None:
        # No transformation in the Dispatcher itself — that's the
        # sink's job (HTML for Telegram, mrkdwn for Slack). Dispatcher
        # is pure routing.
        sink = AsyncMock()
        dispatcher = NotificationDispatcher(sinks=[sink])
        notif = _make_notification()

        await dispatcher.send(notif)

        sink.send.assert_awaited_once_with(notif)


class TestDispatcherPartialFailure:
    @pytest.mark.asyncio
    async def test_send_continues_after_sink_failure(self) -> None:
        # The contract that justifies multi-channel: if Telegram is
        # down, Slack delivers. The first sink raises; the second
        # must STILL receive the notification.
        failing = _FailingSink(RuntimeError("Telegram is down"))
        healthy = _RecordingSink()
        dispatcher = NotificationDispatcher(sinks=[failing, healthy])

        notif = _make_notification()
        await dispatcher.send(notif)  # must not raise

        assert healthy.received == [notif]

    @pytest.mark.asyncio
    async def test_send_does_not_raise_when_all_sinks_fail(self) -> None:
        # Even when EVERY sink fails, the Dispatcher swallows.
        # Rationale: the caller (HealthCheckService wrapper) has
        # already done its primary job (computed findings, rendered
        # the CLI table). Failing the whole command because
        # notifications didn't deliver punishes the operator twice:
        # they don't get the message AND their CLI exits non-zero.
        # The structured ``notify.dispatcher.sink_failed`` events
        # are how ops-side observability sees the failure.
        dispatcher = NotificationDispatcher(
            sinks=[
                _FailingSink(RuntimeError("a")),
                _FailingSink(RuntimeError("b")),
            ],
        )
        await dispatcher.send(_make_notification())  # must not raise

    @pytest.mark.asyncio
    async def test_send_logs_warning_per_failed_sink(self) -> None:
        # Each per-sink failure must emit a distinct structlog event
        # so ops-side dashboards can count "delivery failures by
        # channel". A single rolled-up event would lose the channel
        # attribution.
        #
        # ``structlog.testing.capture_logs`` is the right capture
        # tool here (not pytest ``caplog``): structlog's processor
        # chain does NOT route through stdlib ``logging`` by default
        # in this project, so ``caplog`` sees zero records even when
        # the events are emitted. Same pattern as
        # ``test_reporting.py`` malformed-row tests.
        from structlog.testing import capture_logs

        dispatcher = NotificationDispatcher(
            sinks=[
                _FailingSink(RuntimeError("boom-1")),
                _FailingSink(RuntimeError("boom-2")),
            ],
        )
        with capture_logs() as captured:
            await dispatcher.send(_make_notification())

        failed_events = [
            log
            for log in captured
            if log.get("event") == "notify.dispatcher.sink_failed"
            and log.get("log_level") == "warning"
        ]
        assert len(failed_events) == 2
        # Each event carries the failing sink's class name so
        # downstream dashboards can attribute per-channel.
        assert all(e["sink"] == "_FailingSink" for e in failed_events)
        # Error strings round-trip distinctly so an operator can
        # see "boom-1" vs "boom-2" in the structured log stream.
        errors = sorted(e["error"] for e in failed_events)
        assert errors == ["boom-1", "boom-2"]


class TestDispatcherIsEnabled:
    def test_is_enabled_true_when_sinks_present(self) -> None:
        dispatcher = NotificationDispatcher(sinks=[_RecordingSink()])
        assert dispatcher.is_enabled is True

    def test_is_enabled_false_when_no_sinks(self) -> None:
        # CLI checks this to skip the "computing notification"
        # work entirely when there's nothing to deliver to —
        # e.g. building the summary text. The check is a cheap
        # property, not a method, because callers will want to
        # short-circuit before paying for the render.
        dispatcher = NotificationDispatcher(sinks=[])
        assert dispatcher.is_enabled is False


class TestDispatcherFromSettings:
    def test_from_settings_includes_telegram_when_configured(self) -> None:
        # When Telegram envs are set, Dispatcher.from_settings must
        # include a TelegramSink. Pin this so a refactor that drops
        # the wiring doesn't silently strip Telegram delivery from
        # every consumer.
        from pathlib import Path

        from yadirect_agent.config import Settings
        from yadirect_agent.services.notify.telegram import TelegramSink

        settings = Settings(
            yandex_direct_token=SecretStr("x"),
            yandex_metrika_token=SecretStr("x"),
            telegram_bot_token=SecretStr("123:ABC"),
            telegram_chat_id="42",
            audit_log_path=Path("/tmp/audit.jsonl"),
            agent_policy_path=Path("/tmp/policy.yml"),
            agent_max_daily_budget_rub=10_000,
        )
        dispatcher = NotificationDispatcher.from_settings(settings)
        assert dispatcher.is_enabled is True
        # Inspect the sink list to confirm Telegram is present. The
        # check uses isinstance because the Dispatcher exposes its
        # sinks tuple for caller-side debugging (e.g. ``doctor``
        # listing configured channels in a follow-up).
        assert any(isinstance(s, TelegramSink) for s in dispatcher.sinks)

    def test_from_settings_returns_empty_dispatcher_when_unconfigured(self) -> None:
        # No Telegram envs → Dispatcher with zero sinks. NOT None,
        # because the caller pattern is ``dispatcher.send(...)``
        # unconditionally; returning None would force every caller
        # to ``if dispatcher is not None`` guard. Empty Dispatcher
        # + ``send`` is no-op is the right ergonomics.
        from pathlib import Path

        from yadirect_agent.config import Settings

        settings = Settings(
            yandex_direct_token=SecretStr("x"),
            yandex_metrika_token=SecretStr("x"),
            telegram_bot_token=None,
            telegram_chat_id=None,
            audit_log_path=Path("/tmp/audit.jsonl"),
            agent_policy_path=Path("/tmp/policy.yml"),
            agent_max_daily_budget_rub=10_000,
        )
        dispatcher = NotificationDispatcher.from_settings(settings)
        assert dispatcher.is_enabled is False
        assert tuple(dispatcher.sinks) == ()


class TestDispatcherSinksProperty:
    def test_sinks_is_immutable_view(self) -> None:
        # The internal sink list must not be mutable through the
        # public ``sinks`` property. A consumer that did
        # ``dispatcher.sinks.append(...)`` to inject a sink at runtime
        # would create a hidden state machine. Return a tuple so the
        # immutability is type-level.
        sink = _RecordingSink()
        dispatcher = NotificationDispatcher(sinks=[sink])
        assert isinstance(dispatcher.sinks, tuple)
        # Construction copies the input — caller mutating their list
        # post-construction does not affect the Dispatcher.
        original_input: list[Any] = [sink]
        dispatcher2 = NotificationDispatcher(sinks=original_input)
        original_input.clear()
        assert len(dispatcher2.sinks) == 1
