"""M18 notification & approval pipeline.

Per-sink implementations live in submodules:

- ``telegram`` — TelegramSink (M18 slice 1, shipped). Outbound
  notifications via Bot API; no inline keyboards / approvals yet.
- ``slack`` — SlackSink (M18 slice 2, deferred). Incoming Webhook +
  ``/yadirect-approve <plan_id>`` slash command.
- ``email`` — EmailSink (M18 slice 3, deferred). SMTP for
  weekly / monthly digests where Telegram would be too noisy.
- ``chat`` — ChatSink (M18 slice 4, deferred). MCP-tool result
  fallback when no other sink is configured.

The dispatcher (``services/notify/dispatcher.py``, deferred to
slice 2) routes events by severity → sinks per the operator's
``agent_policy.yml``. Slice 1 ships only the Telegram sink + a
``notify test`` CLI command for verification; future slices
gradually fill in the routing layer + the approval flow
(M18.2 / M18.3).
"""

from __future__ import annotations

__all__: list[str] = []
