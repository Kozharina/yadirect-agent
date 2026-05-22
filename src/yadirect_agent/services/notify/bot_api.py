"""Shared Bot API constants (single source of truth).

Both the production sink (``services/notify/telegram.py``) and the
slice-4 setup wizard helpers (``services/notify/setup_wizard.py``)
talk to ``api.telegram.org``. Pinning the base URL in one place
avoids the silent-drift trap where Telegram changes the host and
two files need synchronous edits (with conflict potential).

The constant is public (no leading underscore) — the import shape
``from .bot_api import BOT_API_BASE`` is what makes the dependency
type-level visible. Tests still mock against the same literal value
via respx, so test isolation is unaffected.
"""

from __future__ import annotations

# Same hostname Telegram has used since the Bot API launched (2015).
# If Telegram ever ships a v2 base URL, this is the one edit needed.
BOT_API_BASE = "https://api.telegram.org"


__all__ = ["BOT_API_BASE"]
