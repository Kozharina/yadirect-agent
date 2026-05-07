"""Notification DTO for the M18 notify pipeline.

A `Notification` is the unit a `NotifySink` (Telegram, Slack,
email, chat) consumes and renders into its medium. Produced by:

- The `HealthCheckService` translating each `Finding` (slice 2 of
  M18 — wiring into the dispatcher).
- M19 rollback / M20 explain decisions.
- Future M5 / M11 / M16 events (calendar bumps, A/B conclusions,
  bid-strategy switches).

Design choices, mirroring `models/health.py:Finding`:

- Frozen dataclass, not pydantic. Produced internally; never
  deserialised from a wire format. Frozen catches the "let me
  bump severity for the demo" anti-pattern.
- `Severity` is reused from `models/health.py` so the operator's
  mental model ("HIGH means I should look NOW") is consistent
  across health findings and notifications.
- `actions: tuple[str, ...]` is a tuple, not a list — slice 2's
  inline-keyboard serialiser (apply / reject buttons) must not be
  able to mutate it mid-render. Tuple makes the immutability a
  type-level invariant. Default empty so slice 1 read-only sinks
  ignore it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .health import Severity


@dataclass(frozen=True)
class Notification:
    """A single notification routed through the M18 pipeline.

    Five fields, deliberately small:

    - ``severity`` — how loud to be; same `Severity` enum the
      health-check pipeline uses.
    - ``title`` — one-line summary for the medium's "headline"
      slot (Telegram first line, email subject, Slack message
      header).
    - ``body`` — multi-line operator-readable description. Plain
      text by default; sinks that support markup (Telegram HTML,
      Slack mrkdwn) escape and apply formatting at render time.
    - ``actions`` — optional tuple of action descriptors. Slice 1
      sinks ignore this field entirely; slice 2 (M18.2) uses it
      to render inline-keyboard buttons that map to
      ``apply-plan`` / ``reject-plan`` / ``why-plan`` flows.
    """

    severity: Severity
    title: str
    body: str
    actions: tuple[str, ...] = field(default_factory=tuple)


__all__ = ["Notification"]
