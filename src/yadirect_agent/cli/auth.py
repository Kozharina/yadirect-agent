"""Pure rendering helpers for ``yadirect-agent auth ...`` (M15.3).

The CLI command bodies (in ``cli/main.py``) stay thin and delegate
all human-facing rendering — and operator-facing Russian strings —
here. Same pattern as ``cli/cost.py``, ``cli/health.py``,
``cli/rationale.py``: keeps the I/O boundary testable in isolation
from typer plumbing.

Secret-handling discipline applies across both human and ``--json``
paths: every renderer here masks ``access_token`` and
``refresh_token`` to a stable ``***`` sentinel. The plaintext lives
only in the keychain and on the wire-call paths inside
``clients/oauth.py``.
"""

# ruff: noqa: RUF001, RUF003
# Operator-facing Russian strings per project language convention
# (see CLAUDE.md). RUF001 / RUF003 flag every Cyrillic ``о`` / ``е``
# / ``Н`` etc. as ambiguous with Latin lookalikes; suppressing at
# file scope keeps the message dictionaries readable.

from __future__ import annotations

from typing import Any

from ..models.auth import TokenSet

# Operator-facing message strings, kept as module-level constants so
# (a) cli/main.py command bodies stay thin and (b) any future
# translation pass has a single greppable surface.
LOGIN_OPENING_BROWSER_HINT = "Открываю браузер для входа в Yandex…"
LOGIN_BROWSER_FALLBACK_HINT = (
    "Если страница не открылась автоматически — нажмите Ctrl-C и проверьте, "
    "что в системе настроен браузер по умолчанию."
)
LOGIN_OAUTH_ERROR_PREFIX = "Ошибка OAuth"
LOGIN_TIMEOUT_HINT = (
    "timeout: не дождались callback от Yandex. "
    "Скорее всего вкладка браузера была закрыта до подтверждения. "
    "Перезапустите команду, истёк таймаут."
)
LOGIN_EXCHANGE_ERROR_PREFIX = "Ошибка обмена кода на токен"
LOGIN_SUCCESS = "Авторизация успешно завершена."
LOGIN_KEYCHAIN_NOTE = "Токен сохранён в OS keychain. Можно запускать остальные команды."
STATUS_NOT_LOGGED_IN = "Не вошли в Yandex. Запустите [bold]yadirect-agent auth login[/bold]."
STATUS_HEADER_LOGGED_IN = "Авторизация: активна"
REVOKE_SUCCESS = "Токен удалён из keychain."


def status_dict(token: TokenSet) -> dict[str, Any]:
    """Return a JSON-friendly summary of the stored token.

    The dict is the SAME shape both ``--json`` and the human-readable
    table draw from, so a regression in one path's masking surfaces
    in the other path's tests too.
    """
    return {
        "token_type": token.token_type,
        "scope": list(token.scope),
        "obtained_at": token.obtained_at.isoformat(),
        "expires_at": token.expires_at.isoformat(),
        # Sentinel masks. Both stay constant strings — anything else
        # (e.g. a length hint) would be a side channel that leaks
        # information about the secret to an attacker reading
        # ``yadirect-agent auth status`` output.
        "access_token": "***",
        "refresh_token": "***",
    }


def render_status_text(token: TokenSet) -> str:
    """Operator-facing summary of the stored token."""
    scope_str = ", ".join(token.scope)
    return (
        f"{STATUS_HEADER_LOGGED_IN}\n"
        f"  scope:        {scope_str}\n"
        f"  token_type:   {token.token_type}\n"
        f"  obtained_at:  {token.obtained_at.isoformat()}\n"
        f"  expires_at:   {token.expires_at.isoformat()}\n"
        "  access_token: ***\n"
        "  refresh_token: ***"
    )


__all__ = [
    "LOGIN_BROWSER_FALLBACK_HINT",
    "LOGIN_EXCHANGE_ERROR_PREFIX",
    "LOGIN_KEYCHAIN_NOTE",
    "LOGIN_OAUTH_ERROR_PREFIX",
    "LOGIN_OPENING_BROWSER_HINT",
    "LOGIN_SUCCESS",
    "LOGIN_TIMEOUT_HINT",
    "REVOKE_SUCCESS",
    "STATUS_HEADER_LOGGED_IN",
    "STATUS_NOT_LOGGED_IN",
    "render_status_text",
    "status_dict",
]
