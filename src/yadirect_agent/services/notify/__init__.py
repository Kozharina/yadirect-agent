"""M18 notification & approval pipeline.

Per-sink implementations live in submodules:

- ``telegram`` — TelegramSink (M18 slice 1, shipped). Outbound
  notifications via Bot API; no inline keyboards / approvals yet.
- ``slack`` — SlackSink (deferred, slice 5 proper). Incoming Webhook +
  ``/yadirect-approve <plan_id>`` slash command.
- ``email`` — EmailSink (deferred, slice 5 proper). SMTP for
  weekly / monthly digests where Telegram would be too noisy.
- ``chat`` — ChatSink (deferred, slice 5 proper). MCP-tool result
  fallback when no other sink is configured.

Cross-cutting modules:

- ``protocol`` — ``NotifySink`` Protocol. One method
  (``async send(Notification) -> None``); 3rd-party sinks /
  test doubles need zero coupling to this codebase.
- ``dispatcher`` — ``NotificationDispatcher`` (M18 slice 5a,
  shipped). Fan-out layer with partial-failure tolerance.
  ``from_settings`` aggregates whatever per-sink
  ``from_settings`` returns non-None for (currently only
  ``TelegramSink``).
- ``render`` — ``health_report_to_notification`` (M18 slice 5a,
  shipped). Folds a ``HealthReport`` into one operator-visible
  summary Notification. Other producers (M19 rollback,
  M20 explain, M21.2 cost enforcement) will get sibling
  rendering functions in this module.

Severity-based routing (``HIGH → Telegram, INFO → Slack`` per
``agent_policy.yml``) lives in the slice 5 proper PR — needs the
remaining sinks to be useful, so deferred until at least one
more sink ships.

The approval flow (inline keyboards, bot polling thread, HMAC-
signed callback_data) lives in M18.2 / M18.3. Slice 5a is
intentionally outbound-only — completes the read-only Phase 1
notification loop; Phase 2 builds on top.
"""

from __future__ import annotations

__all__: list[str] = []
