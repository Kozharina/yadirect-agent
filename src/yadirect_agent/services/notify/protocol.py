"""Structural type for objects the ``NotificationDispatcher`` can deliver to.

A ``NotifySink`` is anything with a single ``async send(Notification)
-> None`` method. The Protocol lets the Dispatcher accept future
sinks (Slack, Email, Chat — M18 slice 5 remainder) without import-
cycle risk and without forcing a common base class.

Why a ``Protocol`` and not a ``typing.ABC``:

- New sinks can be added by 3rd-party packages without subclassing
  anything from this codebase. The ``async send`` shape is the
  contract; nothing else.
- Tests can hand the Dispatcher a 5-line mock class without
  importing ``TelegramSink`` or pulling httpx into the test
  environment. ``test_notify_dispatcher.py`` relies on exactly
  this.
- ``runtime_checkable`` is deliberately NOT applied — the static
  check is enough; making the protocol checkable at runtime would
  invite ``isinstance(sink, NotifySink)`` patterns that obscure the
  intent (we just want the duck to quack).

The contract is intentionally tiny: one async method. Authentication,
HTML escaping, retries, rate limiting — all per-sink concerns the
Dispatcher should never know about.
"""

from __future__ import annotations

from typing import Protocol

from ...models.notification import Notification


class NotifySink(Protocol):
    """Anything the dispatcher can route a Notification to."""

    async def send(self, notification: Notification) -> None:  # pragma: no cover
        """Deliver a notification through this sink's medium.

        Per-sink concerns (transport, escaping, retries, rate
        limiting) live in the implementing class; the Protocol
        only pins the entry-point shape so the Dispatcher can
        fan out without knowing what's underneath.

        Implementers MAY raise on transport failure — the
        ``NotificationDispatcher`` swallows per-sink errors and
        logs ``notify.dispatcher.sink_failed`` so the failure of
        one channel does not block delivery to the others.
        """


__all__ = ["NotifySink"]
