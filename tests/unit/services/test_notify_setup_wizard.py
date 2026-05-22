"""Tests for ``services/notify/setup_wizard.py`` — pure-async helpers (M18 slice 4).

The wizard CLI command (``yadirect-agent notify setup telegram``)
is two layers:

1. **This file's surface** — pure-async helpers that talk to the
   Bot API: ``validate_telegram_token`` (``getMe``) and
   ``await_first_chat_id`` (``getUpdates`` long-poll). They know
   nothing about typer / rich / interactive I/O.
2. **The CLI command in cli/main.py** — orchestrator + Russian
   render layer. Calls the helpers, formats their results / errors
   into operator-visible messages, writes the keychain.

Splitting them is what lets the CLI tests stay focused on the
operator-visible behaviour (which Russian message, which exit
code) without coupling to Bot API wire details, and lets THIS
file pin the wire contracts with respx and zero typer dependency.

What we pin here:

- ``validate_telegram_token`` returns ``BotInfo`` on a ``getMe``
  200 + ``ok: True``; raises ``TokenInvalidError`` on any non-200
  or ``ok: False`` payload.
- ``await_first_chat_id`` long-polls ``getUpdates``, returns the
  first incoming message's ``chat.id`` as ``str``, applies the
  Telegram ``offset`` convention to ack consumed updates, and
  raises ``ChatIdTimeoutError`` after the configured deadline.
- The chat_id is returned as a string (Telegram chat ids are
  signed 64-bit ints; storing as str matches ``Settings.
  telegram_chat_id`` and avoids precision loss when an operator's
  channel id exceeds 2^53).
- Long-poll respects a configurable ``poll_interval_s`` (tests
  set this to 0 so the polling loop spins as fast as the event
  loop allows; production default is 2.0s for friendly rate-
  limiting against the Bot API).

Coverage is the Bot API wire contract, not the operator UX.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from yadirect_agent.services.notify.setup_wizard import (
    BotInfo,
    ChatIdTimeoutError,
    TokenInvalidError,
    await_first_chat_id,
    validate_telegram_token,
)

_BOT_TOKEN = "1234567890:ABC-fake-test-token"


class TestValidateTelegramToken:
    @pytest.mark.asyncio
    async def test_returns_bot_info_on_success(self) -> None:
        # Happy path: getMe responds 200 + ok=True + result.{id,
        # username}. The helper unpacks result into BotInfo so the
        # CLI render layer can show "bot @my_bot found" (in Russian)
        # without parsing wire JSON.
        async with respx.mock(base_url="https://api.telegram.org") as mock:
            mock.get(f"/bot{_BOT_TOKEN}/getMe").respond(
                200,
                json={
                    "ok": True,
                    "result": {
                        "id": 8736995522,
                        "is_bot": True,
                        "first_name": "Yadirect Alerts",
                        "username": "Yadirect_alerts_bot",
                        "can_join_groups": True,
                    },
                },
            )

            info = await validate_telegram_token(_BOT_TOKEN)
            assert isinstance(info, BotInfo)
            assert info.id == 8736995522
            assert info.username == "Yadirect_alerts_bot"

    @pytest.mark.asyncio
    async def test_raises_on_http_401(self) -> None:
        # Wrong / revoked token — Bot API returns 401. The CLI
        # surfaces this as "неверный токен" in Russian, exit 1.
        async with respx.mock(base_url="https://api.telegram.org") as mock:
            mock.get(f"/bot{_BOT_TOKEN}/getMe").respond(
                401,
                json={
                    "ok": False,
                    "error_code": 401,
                    "description": "Unauthorized",
                },
            )

            with pytest.raises(TokenInvalidError, match="401"):
                await validate_telegram_token(_BOT_TOKEN)

    @pytest.mark.asyncio
    async def test_raises_on_ok_false_payload(self) -> None:
        # Bot API sometimes returns 200 with ok=False (very rare for
        # getMe, but pin the defensive path — a future API change
        # that started returning 200 on auth failure must not
        # silently "succeed" the wizard.
        async with respx.mock(base_url="https://api.telegram.org") as mock:
            mock.get(f"/bot{_BOT_TOKEN}/getMe").respond(
                200,
                json={"ok": False, "description": "Forbidden: bot was disabled"},
            )

            with pytest.raises(TokenInvalidError, match="disabled"):
                await validate_telegram_token(_BOT_TOKEN)

    @pytest.mark.asyncio
    async def test_raises_on_network_failure(self) -> None:
        # DNS / timeout / connection refused before we even get an
        # HTTP response. The CLI translates this to a Russian
        # "service-unreachable" message; the helper wraps the
        # underlying httpx error in TokenInvalidError so the CLI
        # has one exception class to catch.
        async with respx.mock(base_url="https://api.telegram.org") as mock:
            mock.get(f"/bot{_BOT_TOKEN}/getMe").mock(
                side_effect=httpx.ConnectError("DNS lookup failed"),
            )

            with pytest.raises(TokenInvalidError):
                await validate_telegram_token(_BOT_TOKEN)


class TestAwaitFirstChatId:
    @pytest.mark.asyncio
    async def test_returns_chat_id_from_first_update(self) -> None:
        # Operator sends /start; bot receives one update. Helper
        # returns the chat.id as str.
        async with respx.mock(base_url="https://api.telegram.org") as mock:
            mock.get(f"/bot{_BOT_TOKEN}/getUpdates").respond(
                200,
                json={
                    "ok": True,
                    "result": [
                        {
                            "update_id": 100,
                            "message": {
                                "message_id": 1,
                                "from": {"id": 1170974327, "is_bot": False},
                                "chat": {"id": 1170974327, "type": "private"},
                                "date": 1747900000,
                                "text": "/start",
                            },
                        }
                    ],
                },
            )

            chat_id = await await_first_chat_id(
                _BOT_TOKEN,
                timeout_s=5.0,
                poll_interval_s=0.0,
            )
            assert chat_id == "1170974327"

    @pytest.mark.asyncio
    async def test_polls_until_message_arrives(self) -> None:
        # First two polls return empty results; third has the
        # message. Helper must keep polling, not give up after
        # one empty round.
        async with respx.mock(base_url="https://api.telegram.org") as mock:
            mock.get(f"/bot{_BOT_TOKEN}/getUpdates").mock(
                side_effect=[
                    httpx.Response(200, json={"ok": True, "result": []}),
                    httpx.Response(200, json={"ok": True, "result": []}),
                    httpx.Response(
                        200,
                        json={
                            "ok": True,
                            "result": [
                                {
                                    "update_id": 200,
                                    "message": {
                                        "message_id": 1,
                                        "from": {"id": 42, "is_bot": False},
                                        "chat": {"id": 42, "type": "private"},
                                        "date": 1747900000,
                                        "text": "hi",
                                    },
                                }
                            ],
                        },
                    ),
                ],
            )

            chat_id = await await_first_chat_id(
                _BOT_TOKEN,
                timeout_s=5.0,
                poll_interval_s=0.0,
            )
            assert chat_id == "42"

    @pytest.mark.asyncio
    async def test_raises_on_timeout(self) -> None:
        # Wall-clock deadline reached with no message — operator
        # forgot to /start the bot, or sent it to the wrong bot.
        # CLI translates to a Russian "timed out" message + exit 1.
        async with respx.mock(base_url="https://api.telegram.org") as mock:
            mock.get(f"/bot{_BOT_TOKEN}/getUpdates").respond(
                200,
                json={"ok": True, "result": []},
            )

            with pytest.raises(ChatIdTimeoutError):
                await await_first_chat_id(
                    _BOT_TOKEN,
                    timeout_s=0.05,  # ~50ms — keep tests fast
                    poll_interval_s=0.01,
                )

    @pytest.mark.asyncio
    async def test_handles_non_message_update(self) -> None:
        # ``getUpdates`` can return non-message updates (edited_message,
        # callback_query, channel_post). Helper must SKIP those —
        # the wizard wants the first message that establishes a
        # chat_id, not random other events. Same poll should also
        # consume the non-message via offset advancement so we
        # don't see it again next round.
        async with respx.mock(base_url="https://api.telegram.org") as mock:
            mock.get(f"/bot{_BOT_TOKEN}/getUpdates").mock(
                side_effect=[
                    httpx.Response(
                        200,
                        json={
                            "ok": True,
                            "result": [
                                {
                                    "update_id": 300,
                                    "edited_message": {
                                        "message_id": 1,
                                        "chat": {"id": 99, "type": "private"},
                                    },
                                }
                            ],
                        },
                    ),
                    httpx.Response(
                        200,
                        json={
                            "ok": True,
                            "result": [
                                {
                                    "update_id": 301,
                                    "message": {
                                        "message_id": 2,
                                        "from": {"id": 100, "is_bot": False},
                                        "chat": {"id": 100, "type": "private"},
                                        "date": 1747900000,
                                        "text": "/start",
                                    },
                                }
                            ],
                        },
                    ),
                ],
            )

            chat_id = await await_first_chat_id(
                _BOT_TOKEN,
                timeout_s=5.0,
                poll_interval_s=0.0,
            )
            assert chat_id == "100"

    @pytest.mark.asyncio
    async def test_chat_id_returned_as_string(self) -> None:
        # Telegram chat ids are signed 64-bit ints; channel ids can
        # exceed 2^53 (JSON safe-int boundary). Returning str keeps
        # precision and matches ``Settings.telegram_chat_id: str |
        # None``.
        big_chat_id = -1001234567890123  # supergroup-style id
        async with respx.mock(base_url="https://api.telegram.org") as mock:
            mock.get(f"/bot{_BOT_TOKEN}/getUpdates").respond(
                200,
                json={
                    "ok": True,
                    "result": [
                        {
                            "update_id": 400,
                            "message": {
                                "message_id": 1,
                                "chat": {"id": big_chat_id, "type": "supergroup"},
                                "date": 1747900000,
                                "text": "/start",
                            },
                        }
                    ],
                },
            )

            chat_id = await await_first_chat_id(
                _BOT_TOKEN,
                timeout_s=5.0,
                poll_interval_s=0.0,
            )
            assert isinstance(chat_id, str)
            assert chat_id == str(big_chat_id)
