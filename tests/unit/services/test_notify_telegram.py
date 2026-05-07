"""Tests for ``TelegramSink`` notification sink (M18 slice 1).

Covers two layers:

1. ``Notification`` model — frozen dataclass round-trip pin
   (severity validation, optional actions list for future approval
   slices).
2. ``TelegramSink`` send flow — happy path, retry on transient HTTP
   error, no-token / no-chat-id graceful refusal, structured log
   events on send + failure.

Mocks the Bot API via ``respx`` (same pattern as the Direct /
Metrika client tests). No real network in CI; sink tests are
fully offline.
"""

from __future__ import annotations

import httpx
import pytest
import respx
from pydantic import SecretStr

from yadirect_agent.models.health import Severity
from yadirect_agent.models.notification import Notification
from yadirect_agent.services.notify.telegram import TelegramSink

_BOT_TOKEN = SecretStr("123456:ABC-fake-test-token")
_CHAT_ID = "987654321"


class TestNotificationModel:
    def test_construction_pins_required_fields(self) -> None:
        n = Notification(
            severity=Severity.HIGH,
            title="Кампания «brand» сожгла 2400 RUB",
            body="0 conversions over the last 7 days. Pause?",
        )
        assert n.severity == Severity.HIGH
        assert n.title == "Кампания «brand» сожгла 2400 RUB"
        assert "Pause" in n.body
        # Actions field exists but defaults to empty for slice 1.
        # Slice 2 (M18.2 approval) will populate it with apply/reject
        # action descriptors that get rendered as inline-keyboard
        # buttons.
        assert n.actions == ()

    def test_actions_field_is_immutable(self) -> None:
        # Frozen dataclass + tuple field — slice 2's inline-keyboard
        # serialiser must not be able to mutate the actions list
        # mid-render. Pin the immutability at the type level.
        n = Notification(
            severity=Severity.WARNING,
            title="x",
            body="y",
            actions=("apply", "reject"),
        )
        assert isinstance(n.actions, tuple)
        with pytest.raises(AttributeError):
            n.actions = ("hacked",)  # type: ignore[misc]


class TestTelegramSinkSend:
    @pytest.mark.asyncio
    async def test_happy_path_posts_to_send_message(self) -> None:
        # The Bot API endpoint is
        # ``https://api.telegram.org/bot<TOKEN>/sendMessage``. The
        # sink must POST a JSON body containing chat_id, text, and
        # parse_mode. Pin the wire shape so a future refactor can't
        # silently drop the parse_mode (which would break Russian
        # bold / link formatting downstream).
        async with respx.mock(base_url="https://api.telegram.org") as mock:
            route = mock.post(f"/bot{_BOT_TOKEN.get_secret_value()}/sendMessage").respond(
                200,
                json={"ok": True, "result": {"message_id": 42}},
            )

            sink = TelegramSink(bot_token=_BOT_TOKEN, chat_id=_CHAT_ID)
            n = Notification(
                severity=Severity.WARNING,
                title="Test",
                body="Hello from yadirect-agent",
            )
            await sink.send(n)

            assert route.called
            request = route.calls.last.request
            import json as _json

            body = _json.loads(request.content)
            assert body["chat_id"] == _CHAT_ID
            # Operator must see severity + title + body assembled
            # legibly in a single message — the body wraps into a
            # bold-prefixed line with the rest below.
            assert "Test" in body["text"]
            assert "Hello from yadirect-agent" in body["text"]
            # parse_mode pinned: HTML (safer than Markdown for
            # operator-controlled body text containing _, *, [, etc.
            # which Markdown would mis-parse).
            assert body["parse_mode"] == "HTML"

    @pytest.mark.asyncio
    async def test_severity_emoji_in_message(self) -> None:
        # Operator scanning a Telegram chat needs to spot HIGH
        # severity at a glance. Pin a leading severity marker so a
        # regression that dropped it surfaces immediately.
        async with respx.mock(base_url="https://api.telegram.org") as mock:
            route = mock.post(
                f"/bot{_BOT_TOKEN.get_secret_value()}/sendMessage",
            ).respond(200, json={"ok": True, "result": {"message_id": 1}})

            sink = TelegramSink(bot_token=_BOT_TOKEN, chat_id=_CHAT_ID)
            await sink.send(
                Notification(
                    severity=Severity.HIGH,
                    title="High thing",
                    body="b",
                ),
            )
            await sink.send(
                Notification(
                    severity=Severity.WARNING,
                    title="Warn thing",
                    body="b",
                ),
            )
            await sink.send(
                Notification(
                    severity=Severity.INFO,
                    title="Info thing",
                    body="b",
                ),
            )

            import json as _json

            calls = list(route.calls)
            high_text = _json.loads(calls[0].request.content)["text"]
            warn_text = _json.loads(calls[1].request.content)["text"]
            info_text = _json.loads(calls[2].request.content)["text"]
            # Each severity gets a distinct visual marker. Pin the
            # exact emoji set so the operator's eye-trained pattern
            # (red square for HIGH) doesn't drift as we add rules.
            assert high_text.startswith("🔴")
            assert warn_text.startswith("🟡")
            assert info_text.startswith("🔵")

    @pytest.mark.asyncio
    async def test_retries_on_transient_error_then_succeeds(self) -> None:
        # Telegram's Bot API occasionally returns 5xx under load.
        # The sink uses tenacity to retry — pin that 1 transient
        # failure followed by a success results in a single delivered
        # notification, not a propagated exception.
        async with respx.mock(base_url="https://api.telegram.org") as mock:
            route = mock.post(f"/bot{_BOT_TOKEN.get_secret_value()}/sendMessage").mock(
                side_effect=[
                    httpx.Response(503, json={"ok": False, "error_code": 503}),
                    httpx.Response(200, json={"ok": True, "result": {"message_id": 1}}),
                ],
            )

            sink = TelegramSink(bot_token=_BOT_TOKEN, chat_id=_CHAT_ID)
            await sink.send(
                Notification(severity=Severity.INFO, title="t", body="b"),
            )

            assert route.call_count == 2  # one retry

    @pytest.mark.asyncio
    async def test_raises_on_persistent_failure(self) -> None:
        # If Telegram is genuinely down (or the token is wrong),
        # the sink must raise after the retry budget is exhausted.
        # Silently swallowing the error would leave the operator
        # thinking the notification went through. Loud failure is
        # the right contract — caller (Dispatcher) can fall back
        # to other sinks.
        async with respx.mock(base_url="https://api.telegram.org") as mock:
            mock.post(f"/bot{_BOT_TOKEN.get_secret_value()}/sendMessage").respond(
                401,
                json={"ok": False, "error_code": 401, "description": "Unauthorized"},
            )

            sink = TelegramSink(bot_token=_BOT_TOKEN, chat_id=_CHAT_ID)
            with pytest.raises(httpx.HTTPStatusError):
                await sink.send(
                    Notification(severity=Severity.INFO, title="t", body="b"),
                )

    def test_constructor_rejects_empty_token(self) -> None:
        # An empty token is a config bug, not a runtime condition.
        # Reject at construction so the operator sees a clear error
        # at startup, not a 401 at first notification.
        with pytest.raises(ValueError, match="bot_token"):
            TelegramSink(bot_token=SecretStr(""), chat_id=_CHAT_ID)

    def test_constructor_rejects_empty_chat_id(self) -> None:
        with pytest.raises(ValueError, match="chat_id"):
            TelegramSink(bot_token=_BOT_TOKEN, chat_id="")

    @pytest.mark.asyncio
    async def test_from_settings_returns_none_when_unconfigured(self) -> None:
        # ``TelegramSink.from_settings(settings)`` is the canonical
        # construction path used by CLI and (later) Dispatcher.
        # When either bot_token or chat_id is missing, return None
        # so the caller can ``if sink is None: skip`` without
        # try/except. Mirrors the ``HealthHistoryStore.from_settings``
        # pattern (always returns store) but here None is a valid
        # "feature disabled" state.
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
        assert TelegramSink.from_settings(settings) is None

    @pytest.mark.asyncio
    async def test_from_settings_returns_sink_when_both_configured(self) -> None:
        from pathlib import Path

        from yadirect_agent.config import Settings

        settings = Settings(
            yandex_direct_token=SecretStr("x"),
            yandex_metrika_token=SecretStr("x"),
            telegram_bot_token=SecretStr("123:ABC"),
            telegram_chat_id="42",
            audit_log_path=Path("/tmp/audit.jsonl"),
            agent_policy_path=Path("/tmp/policy.yml"),
            agent_max_daily_budget_rub=10_000,
        )
        sink = TelegramSink.from_settings(settings)
        assert sink is not None
        assert isinstance(sink, TelegramSink)
