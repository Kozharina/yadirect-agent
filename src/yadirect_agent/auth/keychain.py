"""``KeyringTokenStore`` — OAuth TokenSet ↔ OS keychain (M15.3).

One slot, one JSON blob. The store holds the entire ``TokenSet``
under a single ``(service, username)`` pair so saving and reading
are atomic operations against the keyring backend; splitting access
and refresh across separate slots would open a TOCTOU window where
a crash between the two writes leaves the keychain inconsistent.

All three public methods are defensive:

- ``save`` writes the full TokenSet, overwriting any prior value.
- ``load`` returns ``None`` when the slot is empty, when the JSON
  is corrupt, or when the JSON parses but fails ``TokenSet``
  validation. Each of these maps to "operator must re-login" —
  surfacing them as exceptions would force every caller to handle
  three flavours of "no usable token", which is the same path.
- ``delete`` is idempotent: deleting a non-existent slot is a no-op,
  not an error. ``yadirect-agent auth logout`` always exits zero on
  the no-op path.

The method is named ``delete`` rather than ``revoke``: it removes
the local keychain slot only. Yandex OAuth has no public revocation
endpoint, so a true server-side revoke is impossible from the CLI;
the refresh token Yandex issued remains valid until manual
revocation at https://yandex.ru/profile/access. The method name
reflects what we actually do — delete a local secret — instead of
implying a server-side action we cannot take.

The keychain backend is auto-detected by the ``keyring`` package:
Keychain on macOS, Credential Manager on Windows, Secret Service
(KWallet / GNOME Keyring) on Linux. Headless / Docker / CI fall
back to env vars in ``Settings`` (M15.3 layer 7).
"""

from __future__ import annotations

import json
from typing import Any

import keyring
import keyring.errors
import structlog
from pydantic import ValidationError

from ..models.auth import TokenSet

# Operators clear keychain entries by hand on each OS — Keychain
# Access on macOS, secret-tool on Linux, the Credential Manager
# UI on Windows. Pin the identifiers so a refactor cannot quietly
# orphan past entries.
KEYRING_SERVICE_NAME = "yadirect-agent"
KEYRING_USERNAME = "oauth"


class KeyringTokenStore:
    """Stores a single ``TokenSet`` in the OS keychain.

    Stateless wrapper — no instance fields beyond the structlog
    logger — so a freshly-constructed store always reflects current
    keychain contents. Tests inject the in-memory backend via
    ``monkeypatch`` against the global ``keyring`` module functions.
    """

    def __init__(self) -> None:
        self._logger = structlog.get_logger(__name__).bind(component="keychain")

    def save(self, token: TokenSet) -> None:
        """Write the TokenSet to the keychain, overwriting any prior value.

        Serialises via ``TokenSet.to_storage_dict`` (which exposes
        secret values explicitly — the keychain IS the secure
        store). The whole blob goes into one slot atomically per
        the keyring backend's contract.
        """
        payload = json.dumps(token.to_storage_dict())
        keyring.set_password(KEYRING_SERVICE_NAME, KEYRING_USERNAME, payload)
        self._logger.info(
            "keychain.token_saved",
            scope=list(token.scope),
            expires_at=token.expires_at.isoformat(),
        )

    def load(self) -> TokenSet | None:
        """Read the TokenSet from the keychain, or ``None`` if absent / unusable.

        "Unusable" covers four cases collapsed into one return
        path — missing slot, corrupt JSON, validation failure,
        and **no usable backend at all** (CI Linux without
        keyrings.alt, Docker without dbus, ``keyrings.alt``
        absent on a stripped-down server) — all of which point
        to the same operator action: re-run
        ``yadirect-agent auth login`` (or, for the no-backend
        case, fall back to env-var tokens). Surfacing them as
        four different exceptions would force every caller to
        handle the same recovery path four times.

        ``keyring.errors.KeyringError`` is the base class for
        all keyring-side failures (NoKeyringError, KeyringLocked,
        InitError); catching the base keeps us forward-compatible
        with future keyring versions.
        """
        try:
            raw = keyring.get_password(KEYRING_SERVICE_NAME, KEYRING_USERNAME)
        except keyring.errors.KeyringError:
            self._logger.warning("keychain.backend_unavailable")
            return None
        if raw is None:
            return None
        try:
            data: dict[str, Any] = json.loads(raw)
        except json.JSONDecodeError:
            self._logger.warning("keychain.payload_corrupt_json")
            return None
        try:
            return TokenSet.from_storage_dict(data)
        except ValidationError:
            self._logger.warning("keychain.payload_failed_validation")
            return None

    def delete(self) -> None:
        """Delete the TokenSet from the keychain (idempotent).

        ``yadirect-agent auth logout`` calls this; running it twice
        in a row, or on a fresh install, must not raise. The
        ``PasswordDeleteError`` path from ``keyring.delete_password``
        is the "no record" signal we swallow.

        The method removes the LOCAL slot only. Yandex OAuth has no
        public revocation endpoint, so the refresh token remains
        valid server-side until manually revoked at
        https://yandex.ru/profile/access. ``delete`` reflects what
        we actually do; a name like ``revoke`` would imply a
        server-side action we cannot take.
        """
        try:
            keyring.delete_password(KEYRING_SERVICE_NAME, KEYRING_USERNAME)
            self._logger.info("keychain.token_deleted")
        except keyring.errors.PasswordDeleteError:
            self._logger.info("keychain.delete_noop_no_record")


__all__ = [
    "KEYRING_SERVICE_NAME",
    "KEYRING_USERNAME",
    "KeyringTokenStore",
]
