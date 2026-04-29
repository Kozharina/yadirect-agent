"""Built-in scheduler — cross-platform daily + hourly agent runs (M15.6).

Per-platform implementations live in submodules:
- ``macos`` — LaunchAgent (slice 1, shipped).
- ``linux`` — systemd --user timer (slice 2, deferred).
- ``windows`` — Task Scheduler (slice 3, deferred).

The CLI surface (``yadirect-agent schedule install / status /
remove``) detects the platform via ``sys.platform`` and dispatches
to the appropriate submodule. Linux / Windows in slice 1 surface
a clear "not yet supported, see slice 2/3" message rather than
half-noop'ing.
"""

from __future__ import annotations

__all__: list[str] = []
