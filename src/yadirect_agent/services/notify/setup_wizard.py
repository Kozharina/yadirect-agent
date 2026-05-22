"""Pure-async helpers for the M18.4 ``notify setup telegram`` wizard.

Two responsibilities live here, both Bot API wire concerns:

1. **``validate_telegram_token``** — verifies the operator-supplied
   token by calling ``GET /bot{token}/getMe``. Returns ``BotInfo``
   (id + username) on success so the CLI render layer can show
   "found bot @username" without re-parsing wire JSON. Raises
   ``TokenInvalidError`` on any failure (4xx / 5xx / ok=False /
   network) — the CLI maps that one exception class to a single
   Russian "invalid token / unreachable" message.

2. **``await_first_chat_id``** — long-polls ``GET /bot{token}/
   getUpdates`` until the operator sends a message to the bot (any
   message — typically ``/start``), then returns the resulting
   ``chat.id`` as a string. Raises ``ChatIdTimeoutError`` on a
   wall-clock deadline so the wizard can prompt the operator to
   retry instead of hanging forever.

Why a separate module from ``cli/main.py``:

- **Test isolation.** These helpers are exercised against respx
  mocks with zero typer / rich imports; the CLI tests can in turn
  monkeypatch them out and stay focused on operator-visible
  behavior (which Russian message, which exit code).
- **Reusable substrate.** A future ``mcp`` tool that surfaces the
  same wizard inside Claude Desktop chat would call these helpers
  directly; pulling them out of the CLI keeps that path open.
- **Single responsibility.** The CLI orchestrates 5 wizard steps
  (BotFather instructions → token prompt → validate → chat-id
  capture → save + test-send). Embedding 200 lines of wire logic
  inline would obscure the operator flow.

The chat_id is returned as ``str`` deliberately: Telegram chat ids
are signed 64-bit ints, and supergroup ids (-100...) can exceed
the JSON safe-int boundary 2^53 on some runtimes. ``str`` round-
trips exactly and matches ``Settings.telegram_chat_id: str | None``.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import httpx
import structlog

from .bot_api import BOT_API_BASE

_log = structlog.get_logger(component="services.notify.setup_wizard")

# Default sleep BETWEEN long-poll cycles when the wizard is waiting
# for the operator's first message. The 2-second floor is a safety
# net against tight retry loops on transient 5xx; under normal
# operation the SERVER-side long-poll (see _LONG_POLL_SERVER_S
# below) holds the connection so this sleep is only paid when the
# server returns immediately (which happens only on error). Tests
# override to 0.0 to spin as fast as the event loop allows.
_DEFAULT_POLL_INTERVAL_S = 2.0

# Server-side long-poll budget — passed as the ``timeout`` query
# parameter to Telegram's ``/getUpdates``. Telegram holds the
# connection for up to this many seconds until a message arrives or
# the timer expires (whichever comes first). 30 s is the Telegram-
# recommended long-poll value; combined with our wall-clock deadline
# in ``await_first_chat_id``, it means we poll the server ~4 times
# over a 120-s wizard window instead of ~60 times with timeout=0.
# Telegram caps the value at 50 per the Bot API spec.
_LONG_POLL_SERVER_S = 30

# Per-call HTTP timeout for the ``getUpdates`` long-poll. MUST exceed
# ``_LONG_POLL_SERVER_S`` so the client doesn't tear down the
# connection while the server is still legitimately holding it; the
# 5-s buffer absorbs network jitter without making operator-visible
# delays meaningfully longer.
_PER_POLL_TIMEOUT_S = _LONG_POLL_SERVER_S + 5.0

# Per-call HTTP timeout for one-shot getMe. Short — if Telegram
# can't answer a simple identity call in 10s, the operator's
# network is the problem and they need to fix it (VPN / firewall).
_GETME_TIMEOUT_S = 10.0


@dataclass(frozen=True)
class BotInfo:
    """Minimal bot identity returned by ``/getMe``.

    Frozen because the CLI passes this around for rendering and
    nothing should mutate it mid-wizard. Only the two fields the
    render layer actually uses (``id`` for "saved" telemetry,
    ``username`` for the t.me link the operator clicks to start
    their bot).
    """

    id: int
    username: str


class TokenInvalidError(Exception):
    """Raised when ``validate_telegram_token`` cannot prove the token works.

    Collapses four sub-causes (HTTP 401/403/4xx, ok=False payload,
    transport failure, malformed response) into one exception class
    because the CLI's recovery path is the same for all four — show
    a Russian "invalid or unreachable" message + exit 1; let the
    operator re-run the wizard after they've fixed their token /
    network.
    """


class ChatIdTimeoutError(Exception):
    """Raised when ``await_first_chat_id`` hits its deadline without seeing a message.

    Operator forgot to ``/start`` the bot, or sent the message to
    the wrong bot. CLI shows a Russian "timed out" message + exit 1;
    operator re-runs the wizard.
    """


async def validate_telegram_token(bot_token: str) -> BotInfo:
    """Verify the token by calling ``/getMe``; return BotInfo or raise.

    Wraps every failure mode in ``TokenInvalidError`` with a short
    English ``str`` payload (HTTP status, ``ok=False`` description,
    or network exception class name). The CLI uses ``str(exc)``
    only for debug logs — the operator-facing Russian message is
    hard-coded in the render layer because the wire-level detail
    is rarely actionable for the operator.
    """
    url = f"/bot{bot_token}/getMe"
    try:
        async with httpx.AsyncClient(base_url=BOT_API_BASE, timeout=_GETME_TIMEOUT_S) as client:
            response = await client.get(url)
    except httpx.HTTPError as exc:
        _log.warning(
            "notify.setup.getme_transport_failed",
            error_class=type(exc).__name__,
        )
        msg = f"transport failure: {type(exc).__name__}"
        raise TokenInvalidError(msg) from exc

    if response.status_code != 200:
        _log.warning(
            "notify.setup.getme_http_error",
            status=response.status_code,
        )
        msg = f"HTTP {response.status_code} from Bot API getMe"
        raise TokenInvalidError(msg)

    try:
        data: dict[str, Any] = response.json()
    except ValueError as exc:
        msg = "Bot API getMe returned non-JSON body"
        raise TokenInvalidError(msg) from exc

    if not data.get("ok"):
        description = data.get("description", "unknown Bot API error")
        _log.warning("notify.setup.getme_ok_false", description=description)
        msg = f"Bot API rejected token: {description}"
        raise TokenInvalidError(msg)

    result = data.get("result") or {}
    bot_id = result.get("id")
    username = result.get("username")
    if not isinstance(bot_id, int) or not isinstance(username, str) or not username:
        # Defensive — getMe always returns these for a valid bot,
        # but if Telegram ever ships a wire change, fail loud
        # rather than the wizard silently saving an unusable token.
        msg = "Bot API getMe returned malformed result"
        raise TokenInvalidError(msg)

    return BotInfo(id=bot_id, username=username)


async def await_first_chat_id(
    bot_token: str,
    *,
    timeout_s: float,
    poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
) -> str:
    """Long-poll getUpdates until a chat-establishing message arrives.

    Returns the message's ``chat.id`` as ``str``. Skips non-message
    updates (``edited_message``, ``callback_query``, ``channel_post``
    edits) — the wizard cares specifically about a new message
    that proves the operator can reach the bot. The wizard prompts
    the operator to send ``/start``; in practice the very first
    update is the one we want.

    Telegram's ``offset`` convention: each ``/getUpdates`` returns
    updates with ``update_id`` >= the request's ``offset``. We pass
    ``last_seen_update_id + 1`` so consumed updates don't reappear,
    even if they were not message-type. This keeps the inner loop
    O(new updates) instead of O(all updates seen so far).

    Wall-clock deadline rather than max-attempts because the
    operator's UX is "I have N seconds to switch to Telegram and
    type ``/start``"; ticks of the poll loop are an implementation
    detail.

    ``poll_interval_s`` defaults to 2.0s for friendly rate-limiting.
    Tests pass 0.0 to spin as fast as the event loop allows.
    """
    deadline = time.monotonic() + timeout_s
    offset = 0

    async with httpx.AsyncClient(base_url=BOT_API_BASE, timeout=_PER_POLL_TIMEOUT_S) as client:
        while time.monotonic() < deadline:
            response = await client.get(
                f"/bot{bot_token}/getUpdates",
                # Server-side long-poll: Telegram holds the connection
                # until a message arrives or _LONG_POLL_SERVER_S elapses,
                # whichever comes first. Reduces wizard request volume
                # ~15x compared to ``timeout=0`` short-polling.
                params={"offset": offset, "timeout": _LONG_POLL_SERVER_S},
            )
            if response.status_code == 200:
                try:
                    data: dict[str, Any] = response.json()
                except ValueError:
                    # Non-JSON response — let the loop retry; we
                    # don't want one bad poll to abort the wizard.
                    data = {"ok": False, "result": []}

                if data.get("ok"):
                    for update in data.get("result") or []:
                        if not isinstance(update, dict):
                            continue
                        update_id = update.get("update_id")
                        if isinstance(update_id, int):
                            # Always advance offset past every
                            # update we've seen, even the non-
                            # message ones we're about to skip.
                            offset = update_id + 1
                        message = update.get("message")
                        if not isinstance(message, dict):
                            continue
                        chat = message.get("chat")
                        if not isinstance(chat, dict):
                            continue
                        chat_id = chat.get("id")
                        if isinstance(chat_id, int):
                            return str(chat_id)
            # else: HTTP failure → swallow and retry; the wizard's
            # wall-clock budget bounds the worst case.

            await asyncio.sleep(poll_interval_s)

    raise ChatIdTimeoutError(f"no chat-establishing message within {timeout_s:.0f}s")


__all__ = [
    "BotInfo",
    "ChatIdTimeoutError",
    "TokenInvalidError",
    "await_first_chat_id",
    "validate_telegram_token",
]
