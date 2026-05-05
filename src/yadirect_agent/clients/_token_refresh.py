"""Shared OAuth token-refresh helper for Yandex Direct + Metrika clients.

Both ``DirectApiClient.call`` (after ``AuthError(code=52)``) and
``MetrikaService._request`` (after HTTP 401) follow the same
4-step refresh dance:

1. Load the current ``TokenSet`` from the OS keychain.
2. Call ``oauth.refresh_access_token`` with the refresh half.
3. Persist the new ``TokenSet`` back to keychain.
4. Mirror the new access token into ``Settings`` and rewrite the
   client's ``Authorization`` header so the very next request
   inside the same process uses the fresh token.

Pre-extraction the two clients carried 95%-identical ~50-line
copies of this; the only differences were:

- The HTTP scheme on the ``Authorization`` header (``Bearer`` for
  Direct's JSON-RPC, ``OAuth`` for Metrika's REST).
- Whether structlog logging was emitted (Direct logged each
  branch with ``api.auth_refresh.*`` events; Metrika silently
  swallowed errors).

The helper unifies both. ``scheme`` selects the wire scheme;
``logger`` is now mandatory at the helper boundary so the
auth-refresh path is never silent — operators reading logs can
always tell whether refresh fired and whether it succeeded.
Metrika previously had no log; this is an improvement.

Mirroring both ``yandex_direct_token`` and ``yandex_metrika_token``
on every refresh matches the M15.3 contract: a single OAuth grant
covers both Direct and Metrika scopes (see
``Settings._hydrate_tokens_from_keyring``). A future per-scope
split would change that contract here too.
"""

from __future__ import annotations

from typing import Literal

import httpx
import structlog

from ..config import Settings
from .oauth import refresh_access_token

_log = structlog.get_logger(__name__)

# httpx ``Authorization`` schemes used by the two clients we
# share with. Direct's API v5 expects ``Bearer``; Metrika expects
# the legacy Yandex ``OAuth`` scheme. Pinning as a Literal stops
# a typo at the call site (``"bearer"`` lower-case would silently
# pass ``str``).
TokenScheme = Literal["Bearer", "OAuth"]


async def refresh_settings_token(
    settings: Settings,
    *,
    scheme: TokenScheme,
    httpx_client: httpx.AsyncClient | None = None,
    logger: structlog.stdlib.BoundLogger | None = None,
) -> bool:
    """Refresh keychain TokenSet, mirror to Settings, rewrite httpx header.

    Returns ``True`` when the refresh succeeded and the next call
    against ``httpx_client`` (and any future-process boot reading
    Settings from the keychain) sees the fresh access token.
    Returns ``False`` on every recoverable failure path:

    - No keychain entry (operator never ran ``auth login``).
    - ``KeyringTokenStore.load`` failed (corrupt JSON, backend
      hiccup — the store itself is already defensive but we
      double-guard against future-novel exception classes).
    - ``oauth.refresh_access_token`` raised (refresh token
      revoked at ``yandex.ru/profile/access``, transient network
      blip, Yandex OAuth backend down).

    The caller (Direct / Metrika) surfaces its original
    ``AuthError`` / 401 on ``False`` so operators see the
    actionable cause ("re-run ``yadirect-agent auth login``")
    rather than an opaque retry failure.

    Side effects on success:

    - New ``TokenSet`` persisted to keychain so the NEXT process
      invocation also benefits.
    - ``settings.yandex_direct_token`` AND
      ``settings.yandex_metrika_token`` mirror the new
      access_token (a single OAuth grant covers both scopes per
      ``Settings._hydrate_tokens_from_keyring``).
    - ``httpx_client.headers["Authorization"]`` rewritten with
      the configured ``scheme`` + new token, so the next HTTP
      call inside the same process uses the fresh credential.
      ``httpx_client=None`` skips the rewrite (callers without a
      live client — e.g. service-level retry loops that
      re-instantiate per attempt — get the Settings + keychain
      side-effects only).
    """
    log = logger if logger is not None else _log

    # Lazy import keeps the keyring stack out of every clients/
    # import chain; the module-load cost only pays on the (rare)
    # refresh path.
    from ..auth.keychain import KeyringTokenStore

    store = KeyringTokenStore()
    try:
        token = store.load()
    except Exception as exc:
        # ``KeyringTokenStore.load`` already catches the documented
        # keyring exceptions (KeyringError, JSONDecodeError,
        # ValidationError); this guards against future-novel
        # exception classes from the underlying backend.
        log.warning("api.auth_refresh.skip_keychain_load_failed", error=str(exc))
        return False
    if token is None:
        log.info("api.auth_refresh.skip_no_keychain_token")
        return False

    try:
        new_token = await refresh_access_token(
            refresh_token=token.refresh_token.get_secret_value(),
        )
    except Exception as exc:
        log.warning("api.auth_refresh.failed", error=str(exc))
        return False

    store.save(new_token)
    settings.yandex_direct_token = new_token.access_token
    settings.yandex_metrika_token = new_token.access_token
    if httpx_client is not None:
        httpx_client.headers["Authorization"] = (
            f"{scheme} {new_token.access_token.get_secret_value()}"
        )
    log.info("api.auth_refresh.ok")
    return True


__all__ = ["TokenScheme", "refresh_settings_token"]
