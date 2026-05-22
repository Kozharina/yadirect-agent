"""Telegram Bot API notification sink (M18 slice 1).

Outbound only. Sends ``Notification`` rows to a single
operator-configured chat via the Bot API ``sendMessage`` endpoint.

What slice 1 ships:

- One ``TelegramSink`` class with ``send(Notification) -> None``.
- HTTP via httpx + tenacity retry on transient 5xx and network
  errors (consistent with the Direct / Metrika client retry
  posture). 4 attempts, exp backoff up to 30 s.
- Severity emoji prefix per level (🔴 HIGH / 🟡 WARNING /
  🔵 INFO) so the operator's eye-trained pattern stays
  consistent with the CLI table colours and future Slack /
  email rendering.
- ``parse_mode=HTML`` (not Markdown). Markdown mis-parses
  operator-controlled body text containing ``_``, ``*``,
  ``[`` (campaign names like ``brand_test`` would render
  italicised); HTML escaping is more predictable.
- ``from_settings(settings)`` classmethod returns ``None`` when
  either ``telegram_bot_token`` or ``telegram_chat_id`` is
  missing — caller does ``if sink is None: skip`` without
  try/except. Different from ``HealthHistoryStore.from_settings``
  (always returns store, never None) because for notifications,
  "feature disabled" is a valid state, not an error.

What slice 1 does NOT ship:

- Inline keyboards / callback_data → that's M18.2 approval flow,
  needs a bot polling thread + apply-plan local-socket bridge.
  Big separate scope.
- HMAC anti-injection → M18.3, paired with the approval flow.
- Setup wizard (``yadirect-agent notify setup telegram``) → M18.4.
  Slice 1 reads the token / chat_id from env vars
  (``TELEGRAM_BOT_TOKEN``, ``TELEGRAM_CHAT_ID`` — no
  ``YADIRECT_`` prefix; pydantic-settings without ``env_prefix``
  resolves field names uppercased) via Settings; slice 4 will
  migrate to keyring storage with env-var fallback for headless
  / CI / Docker contexts (same shape as the M15.3 OAuth tokens).
- Dispatcher / routing → M18.1 second part. Slice 1 callers
  construct the sink and call ``send`` directly; slice 2 wires
  the Dispatcher in front of the sinks.

Errors propagate by design — a silently-swallowed send would
leave the operator thinking the notification went through. The
caller (Dispatcher in slice 2) decides whether to fall back to
another sink.
"""

from __future__ import annotations

import html
from typing import TYPE_CHECKING, Any

import httpx
import structlog
from pydantic import SecretStr
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_random_exponential,
)

from ...models.health import Severity
from ...models.notification import Notification
from .bot_api import BOT_API_BASE

if TYPE_CHECKING:
    from ...config import Settings

_log = structlog.get_logger(component="services.notify.telegram")

# Pinned per-severity visual marker. Operator scanning a chat
# spots HIGH at a glance via the colour. The round emoji set
# (red / yellow / blue) is unambiguous and matches the same-
# shape pattern across all three severities.
_SEVERITY_EMOJI: dict[Severity, str] = {
    Severity.HIGH: "🔴",
    Severity.WARNING: "🟡",
    Severity.INFO: "🔵",
}

# Telegram's HTTP statuses that warrant retry. 5xx covers
# transient API issues; 429 is rate limiting (Telegram tells us
# to back off via Retry-After, but tenacity's exp backoff is good
# enough for slice 1 — slice-future enhancement: parse Retry-After
# from the response).
_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})


class _RetryableTelegramError(Exception):
    """Internal sentinel used by tenacity to retry on retryable HTTP statuses.

    Wraps ``httpx.HTTPStatusError`` for the retry decorator. The
    final caller sees ``httpx.HTTPStatusError`` (re-raised after
    the retry budget is exhausted) — same exception class as a
    direct ``response.raise_for_status()`` call, so consumers of
    this module don't have to learn a sink-specific exception.
    """


class TelegramSink:
    """Send `Notification` rows to a single Telegram chat."""

    def __init__(self, *, bot_token: SecretStr, chat_id: str) -> None:
        token_value = bot_token.get_secret_value()
        if not token_value:
            msg = "bot_token must be a non-empty secret; got an empty string"
            raise ValueError(msg)
        if not chat_id:
            msg = "chat_id must be a non-empty string; got empty"
            raise ValueError(msg)
        self._bot_token = bot_token
        self._chat_id = chat_id

    @classmethod
    def from_settings(cls, settings: Settings) -> TelegramSink | None:
        """Construct from `Settings`, or return None when unconfigured.

        "Unconfigured" means either ``telegram_bot_token`` or
        ``telegram_chat_id`` is missing. Returning None lets the
        caller (CLI command, future Dispatcher) treat the
        notification surface as gracefully disabled instead of
        crashing on a fresh install.
        """
        token = settings.telegram_bot_token
        chat_id = settings.telegram_chat_id
        if token is None or chat_id is None:
            return None
        if not token.get_secret_value() or not chat_id:
            return None
        return cls(bot_token=token, chat_id=chat_id)

    async def send(self, notification: Notification) -> None:
        """Render the notification and POST it to Telegram.

        Retries 5xx + 429 up to 4 attempts with exp backoff.
        Persistent failure (4xx other than 429) raises
        ``httpx.HTTPStatusError`` immediately — config bugs
        should not waste retry budget.
        """
        text = self._render_text(notification)
        endpoint = f"/bot{self._bot_token.get_secret_value()}/sendMessage"
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "HTML",
        }

        @retry(
            retry=retry_if_exception_type(
                (httpx.TransportError, _RetryableTelegramError),
            ),
            stop=stop_after_attempt(4),
            wait=wait_random_exponential(multiplier=1.0, max=30.0),
            reraise=True,
        )
        async def _do_send() -> Any:
            async with httpx.AsyncClient(base_url=BOT_API_BASE, timeout=10.0) as client:
                response = await client.post(endpoint, json=payload)
                if response.status_code in _RETRYABLE_STATUSES:
                    # Wrap + raise so tenacity sees a retryable; the
                    # final propagation re-raises the underlying
                    # httpx.HTTPStatusError for caller-side handling.
                    try:
                        response.raise_for_status()
                    except httpx.HTTPStatusError as exc:
                        raise _RetryableTelegramError(str(exc)) from exc
                response.raise_for_status()
                return response

        try:
            await _do_send()
        except _RetryableTelegramError as exc:
            # Retry budget exhausted on a retryable status. Surface
            # the underlying HTTPStatusError so callers see one
            # exception class regardless of whether the failure was
            # transient-then-fatal or fatal from the start.
            cause = exc.__cause__
            _log.warning(
                "notify.telegram.send_failed",
                severity=notification.severity.value,
                title=notification.title,
                error=str(cause or exc),
            )
            if isinstance(cause, httpx.HTTPStatusError):
                raise cause from None
            raise
        except httpx.HTTPStatusError as exc:
            _log.warning(
                "notify.telegram.send_failed",
                severity=notification.severity.value,
                title=notification.title,
                status=exc.response.status_code,
            )
            raise

        _log.info(
            "notify.telegram.sent",
            severity=notification.severity.value,
            title=notification.title,
        )

    @staticmethod
    def _render_text(notification: Notification) -> str:
        """Compose the operator-visible message body.

        Layout (HTML parse_mode):

            <emoji> <b>title</b>
            body

        Title goes bold so it stands out in the chat thread; body
        below in plain (HTML-escaped) text. Future M18.2 inline
        keyboards will piggy-back on this rendering; slice 1
        ignores ``actions`` entirely.

        Both ``title`` and ``body`` are HTML-escaped so a campaign
        name containing ``<`` or ``&`` doesn't break the parse_mode
        contract. Same defensive shape as ``_rich_escape`` in the
        CLI surfaces.
        """
        emoji = _SEVERITY_EMOJI.get(notification.severity, "•")
        title = html.escape(notification.title)
        body = html.escape(notification.body)
        return f"{emoji} <b>{title}</b>\n{body}"


__all__ = ["TelegramSink"]
