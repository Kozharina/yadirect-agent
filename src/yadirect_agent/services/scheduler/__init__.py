"""Built-in scheduler — cross-platform daily + hourly agent runs (M15.6).

Per-platform implementations live in submodules:
- ``macos`` — LaunchAgent (slice 1, shipped).
- ``linux`` — systemd ``--user`` timers (slice 2, shipped).
- ``windows`` — Task Scheduler (slice 3, shipped). M15.6 closed.

The CLI surface (``yadirect-agent schedule install / status /
remove``) detects the platform via ``sys.platform`` and dispatches
to the appropriate submodule.

No shared abstract base / Protocol — decision deliberate after
all three slices landed:

- ``InstallResult`` cardinalities differ across platforms (2
  plist paths on macOS, 4 unit-file paths on Linux, 2 XML paths +
  task names on Windows). A common shape would either lose
  information or grow ``Optional`` fields per-platform.
- ``ScheduleStatus`` shapes are similar (``installed: bool`` +
  per-platform paths) but the path attribute names are
  platform-specific (``daily_plist_path`` vs ``daily_timer_path``
  vs ``daily_xml_path``); collapsing them would lose the
  domain-specific naming that makes operator output readable.
- The lifecycle methods (``install`` / ``status`` / ``remove``)
  are nominally similar but their on-disk + subprocess shapes
  diverge enough that a Protocol would only encode the smallest
  common subset (``install() -> SomeResult``, ``remove() -> None``,
  ``status() -> SomeStatus``), which the CLI dispatch already
  encodes implicitly via three explicit branches.

The duplication between ``macos.py`` / ``linux.py`` / ``windows.py``
is structural, not accidental, and sits at ~10 lines per platform
(``_validate_executable`` + ``_atomic_write``) — below the cost of
an abstraction that would only obscure the per-platform reasoning
docstrings each module carries.
"""

from __future__ import annotations

__all__: list[str] = []
