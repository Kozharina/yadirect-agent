"""Fan-out layer between notification producers and per-medium sinks (M18 slice 5a).

The ``NotificationDispatcher`` is the LAYER, not a sink. It holds
zero-or-more sinks and delivers each ``Notification`` to all of
them, tolerating per-sink failure so that one channel being down
doesn't silently drop the operator's only notification path.

Why a separate layer at all (instead of "just call sink.send"
from each producer):

- Multi-channel becomes a property of the system, not of every
  producer. ``HealthCheckService`` doesn't need to know whether
  the operator wired Telegram, Slack, both, or neither.
- Per-sink failure handling lives in one place. A producer who
  ``try/except`` around every sink call would either duplicate
  the swallow-and-log boilerplate or accidentally fail-fast.
- Future additions (severity-based routing — "INFO to Slack only,
  HIGH to all sinks"; rate limiting; dedup) plug into one well-
  documented seam.

What slice 5a ships:

- Synchronous (in-order) fan-out — ``await sink.send(...)`` for
  each sink in turn. NOT parallel ``asyncio.gather`` because:
  1. Sink count is small (typically 1-3 channels). The wall-clock
     savings from parallelism are <1s on a healthy network.
  2. Sequential failures are easier to log + reason about — the
     N+1th log event reading "sink_failed: TelegramSink" is in the
     same temporal cluster as the human-visible Telegram timeout.
  3. ``asyncio.gather`` with ``return_exceptions=True`` makes the
     error path tricky (which sinks succeeded? in what order did
     they fail?). Slice 5a keeps it boring.
  Parallelism is a slice-future optimisation — when slice 5
  proper lands Slack + Email + Chat sinks the wall-clock budget
  may warrant it.

- ``from_settings(settings)`` aggregates whatever sinks the
  per-sink ``from_settings`` returns non-None for. Currently only
  ``TelegramSink``. Adding a sink to the Dispatcher = adding one
  ``if sink is not None: sinks.append(sink)`` block here; no
  caller-side change.

What slice 5a does NOT ship:

- Severity routing (``HIGH → Telegram, INFO → Slack``) — needs
  an ``agent_policy.yml`` schema knob. Deferred to slice 5 proper.
- Per-sink rate limiting — not a real problem with one sink and
  one notification per ``health`` run. Will become real once
  Slack incoming-webhook lands (Slack's free tier throttles).
- Dedup (same Notification arriving twice in a 5-min window —
  the daily ``schedule run`` shouldn't double-fire the operator
  every morning) — needs persistent state, deferred.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from ...models.notification import Notification
from .protocol import NotifySink

if TYPE_CHECKING:
    from collections.abc import Iterable

    from ...config import Settings

_log = structlog.get_logger(component="services.notify.dispatcher")


class NotificationDispatcher:
    """Hold a list of sinks, fan a Notification out to all of them."""

    def __init__(self, *, sinks: Iterable[NotifySink]) -> None:
        # Copy into a tuple so:
        # 1. The internal list is immutable for the Dispatcher's
        #    lifetime — no surprise mid-run sink injection.
        # 2. Caller mutating the input list post-construction does
        #    not affect us (defensive: tests pin this).
        self._sinks: tuple[NotifySink, ...] = tuple(sinks)

    @property
    def sinks(self) -> tuple[NotifySink, ...]:
        """The configured sinks, in delivery order.

        Returned as a tuple so callers can iterate / inspect
        (e.g. a future ``doctor`` listing of configured channels)
        but cannot mutate.
        """
        return self._sinks

    @property
    def is_enabled(self) -> bool:
        """True if at least one sink is wired in.

        Callers (e.g. CLI ``health``) check this to short-circuit
        the "compose summary text" work when there's nothing to
        deliver to. The Dispatcher's ``send`` is still safe to
        call on a disabled instance — it's a no-op + log.
        """
        return bool(self._sinks)

    @classmethod
    def from_settings(cls, settings: Settings) -> NotificationDispatcher:
        """Assemble a Dispatcher from whatever sinks the Settings configure.

        Sink-presence is per-sink: a ``None`` from ``TelegramSink.
        from_settings`` (env vars missing) silently omits Telegram
        from the sink list. A Dispatcher with zero sinks is still
        a valid object — see ``is_enabled`` for the caller-side
        check.

        Currently aggregates only ``TelegramSink``. Adding Slack /
        Email / Chat is "one more import + one more block" and
        symmetric to this one.
        """
        # Import lazily so a test environment without httpx (or
        # the future Slack/SMTP deps) doesn't pay the import cost.
        from .telegram import TelegramSink

        sinks: list[NotifySink] = []

        telegram = TelegramSink.from_settings(settings)
        if telegram is not None:
            sinks.append(telegram)

        return cls(sinks=sinks)

    async def send(self, notification: Notification) -> None:
        """Deliver the notification to every sink, swallowing per-sink errors.

        Ordering: sinks are awaited in the order they were
        constructed. Failures are logged at WARNING with the sink
        class name + the exception's string form so ops-side
        dashboards can count delivery failures per-channel.

        Never raises. The caller (CLI ``health``, daily ``schedule
        run``, future M19 rollback, M20 explain) is expected to
        have done its primary job before calling us; punishing
        that caller because a notification didn't deliver is the
        wrong contract.
        """
        if not self._sinks:
            _log.debug(
                "notify.dispatcher.skipped_no_sinks",
                severity=notification.severity.value,
                title=notification.title,
            )
            return

        for sink in self._sinks:
            sink_name = type(sink).__name__
            try:
                await sink.send(notification)
            except Exception as exc:
                # ``BLE001 blind-except`` is the right shape here:
                # the Dispatcher cannot know which exception types
                # each sink raises (httpx for Telegram, smtplib for
                # Email, slack_sdk for Slack…). Catching ``Exception``
                # is the documented contract. Per-sink classes still
                # log their own structured event before raising
                # (see ``TelegramSink._do_send``), so we don't lose
                # detail by collapsing here.
                _log.warning(
                    "notify.dispatcher.sink_failed",
                    sink=sink_name,
                    severity=notification.severity.value,
                    title=notification.title,
                    error=str(exc),
                )
                continue
            _log.debug(
                "notify.dispatcher.sink_delivered",
                sink=sink_name,
                severity=notification.severity.value,
                title=notification.title,
            )


__all__ = ["NotificationDispatcher"]
