"""Built-in scheduler — cross-platform daily + hourly agent runs (M15.6).

Per-platform implementations live in submodules:
- ``macos`` — LaunchAgent (slice 1, shipped).
- ``linux`` — systemd ``--user`` timers (slice 2, shipped).
- ``windows`` — Task Scheduler (slice 3, deferred).

The CLI surface (``yadirect-agent schedule install / status /
remove``) detects the platform via ``sys.platform`` and dispatches
to the appropriate submodule. Windows in slices 1+2 surfaces a
clear "shipping in slice 3" message rather than half-noop'ing.

No shared abstract base / Protocol on purpose: macOS and Linux
results have different cardinalities (2 plist paths vs 4 unit-file
paths), so a common ``InstallResult`` would either lose
information or grow ``Optional`` fields. The duplication between
``macos.py`` and ``linux.py`` is structural, not accidental, and
sits at ~10 lines per platform — far below the cost of an
abstraction. Promote to a shared protocol only if slice 3 reveals
a third copy of the same shape; for now, keep each platform
honest and self-contained.
"""

from __future__ import annotations

__all__: list[str] = []
