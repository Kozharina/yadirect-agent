"""``KeyringTelegramStore`` — Telegram (bot_token, chat_id) ↔ OS keychain (M18 slice 4).

The M18.4 setup wizard (``yadirect-agent notify setup telegram``)
writes here; ``Settings._hydrate_tokens_from_keyring`` reads here
when the operator hasn't set ``TELEGRAM_BOT_TOKEN`` /
``TELEGRAM_CHAT_ID`` env-vars (the headless / Docker / CI escape
hatch).

Why a separate store class instead of reusing ``KeyringTokenStore``:

- **Payload shape differs.** OAuth's ``TokenSet`` is 4 secret
  fields + scope + timestamps; Telegram is 2 strings. Forcing
  one class to serialise both pushes union-types into ``load``
  and obscures the per-credential contract.
- **Independent slot.** Different ``KEYRING_USERNAME`` (``"telegram"``
  vs ``"oauth"``) so ``yadirect-agent auth logout`` does not
  silently strip Telegram setup, and a future ``notify setup
  telegram --reset`` does not log the operator out of Yandex.
  Same operator-visible ``KEYRING_TELEGRAM_SERVICE_NAME``
  (``"yadirect-agent"``) keeps both credentials grouped under one
  app name in Keychain Access / Credential Manager.

Single atomic slot: one JSON blob holds both fields. Splitting
across two keychain slots would open a TOCTOU window where a
crash between writes leaves the keychain inconsistent (token
saved, chat_id not — or vice versa). ``Settings`` would then
hydrate one field but not the other, and the resulting half-
configured ``TelegramSink`` would fail at first ``send`` with an
unhelpful error.

Defensive ``load`` collapses four "no usable creds" cases into one
return: missing slot, corrupt JSON, missing required field, backend
unavailable. Same shape as ``KeyringTokenStore.load`` — operator
recovery is identical (re-run wizard or set env-vars), so a single
return path keeps callers simple.

The class is stateless beyond the structlog logger; tests inject
the in-memory keyring backend via monkeypatch against the global
``keyring`` module functions (same fixture as ``test_keychain.py``).
"""

from __future__ import annotations

import json
from typing import Any

import keyring
import keyring.errors
import structlog

# Same project-wide service name as OAuth (M15.3). One root
# identifier per project in Keychain Access / Credential Manager;
# the per-credential differentiator is the USERNAME, not the
# SERVICE. Pinned so a refactor cannot quietly orphan past
# wizard runs.
KEYRING_TELEGRAM_SERVICE_NAME = "yadirect-agent"

# Distinct from ``KEYRING_USERNAME = "oauth"`` so ``auth logout``
# and ``notify setup telegram --reset`` touch independent slots.
# Pinned because operators clear keychain entries by hand —
# changing the literal here orphans the prior wizard run on
# every operator machine.
KEYRING_TELEGRAM_USERNAME = "telegram"


class KeyringTelegramStore:
    """Stores a (bot_token, chat_id) pair in the OS keychain.

    Stateless wrapper — no instance fields beyond the structlog
    logger — so a freshly-constructed store always reflects current
    keychain contents.
    """

    def __init__(self) -> None:
        self._logger = structlog.get_logger(__name__).bind(component="auth.telegram_keychain")

    def save(self, *, bot_token: str, chat_id: str) -> None:
        """Write the (bot_token, chat_id) pair to the keychain.

        Both fields are required; empty values are a wizard-bug
        (validation must have caught them earlier) and we raise
        ``ValueError`` rather than let the keychain hold a useless
        half-record. Symmetric to ``TelegramSink.__init__``'s
        rejection of empty constructor args.
        """
        if not bot_token:
            msg = "bot_token must be a non-empty string; got empty"
            raise ValueError(msg)
        if not chat_id:
            msg = "chat_id must be a non-empty string; got empty"
            raise ValueError(msg)
        payload = json.dumps({"bot_token": bot_token, "chat_id": chat_id})
        keyring.set_password(
            KEYRING_TELEGRAM_SERVICE_NAME,
            KEYRING_TELEGRAM_USERNAME,
            payload,
        )
        # NEVER log the token / chat_id — the keychain IS the
        # secure store; emitting the payload would defeat the
        # whole point. Log just the fact-of-save.
        self._logger.info("telegram_keychain.saved")

    def load(self) -> tuple[str, str] | None:
        """Read the pair from the keychain, or ``None`` if absent / unusable.

        "Unusable" covers four cases collapsed into one return path:
        missing slot, corrupt JSON, JSON parses but lacks a required
        field, and **no usable backend at all** (CI Linux without
        keyrings.alt, Docker without dbus). All four map to the same
        operator action — re-run ``notify setup telegram`` or set
        the env-vars — so surfacing them as four different exceptions
        would force every caller to handle the same recovery path
        four times.
        """
        try:
            raw = keyring.get_password(
                KEYRING_TELEGRAM_SERVICE_NAME,
                KEYRING_TELEGRAM_USERNAME,
            )
        except keyring.errors.KeyringError:
            self._logger.warning("telegram_keychain.backend_unavailable")
            return None
        if raw is None:
            return None
        try:
            data: dict[str, Any] = json.loads(raw)
        except json.JSONDecodeError:
            self._logger.warning("telegram_keychain.payload_corrupt_json")
            return None
        bot_token = data.get("bot_token")
        chat_id = data.get("chat_id")
        if not isinstance(bot_token, str) or not isinstance(chat_id, str):
            # Missing field, wrong type, or empty string after a
            # manual edit — same recovery path as corrupt JSON.
            self._logger.warning("telegram_keychain.payload_missing_fields")
            return None
        if not bot_token or not chat_id:
            self._logger.warning("telegram_keychain.payload_empty_fields")
            return None
        return bot_token, chat_id

    def delete(self) -> None:
        """Delete the keychain entry (idempotent).

        ``notify setup telegram --reset`` calls this; running it
        twice in a row, or on a fresh install, must not raise.
        ``PasswordDeleteError`` from ``keyring.delete_password`` is
        the "no record" signal we swallow.

        Local-only: this removes the keychain entry. The Telegram
        bot itself remains valid on Telegram's side until the
        operator revokes the token via ``@BotFather``. The method
        name reflects what we actually do; ``revoke`` would imply
        a server-side action we cannot take from the keychain
        layer.
        """
        try:
            keyring.delete_password(
                KEYRING_TELEGRAM_SERVICE_NAME,
                KEYRING_TELEGRAM_USERNAME,
            )
            self._logger.info("telegram_keychain.deleted")
        except keyring.errors.PasswordDeleteError:
            self._logger.info("telegram_keychain.delete_noop_no_record")


__all__ = [
    "KEYRING_TELEGRAM_SERVICE_NAME",
    "KEYRING_TELEGRAM_USERNAME",
    "KeyringTelegramStore",
]
