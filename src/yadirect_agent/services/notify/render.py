"""Render a ``HealthReport`` into one summary ``Notification`` (M18 slice 5a).

This module is the bridge between the producer (``HealthCheckService``)
and the per-medium sinks (Telegram today; Slack / Email / Chat later).
It owns one decision: how do N findings fold into one operator-visible
message?

Contract:

- One summary Notification per report (not one-per-finding). The
  operator inbox has finite attention; 20 findings ⇒ 20 pings ⇒ the
  channel gets silenced within a week. Summary keeps the channel
  trustworthy.
- ``None`` when the report is empty. "No news" alerts train the
  operator to ignore the channel; we send nothing.
- Severity = max across all findings. The operator scans severity
  first to decide urgency; preserving the most-actionable signal
  is correct.
- Title carries scale ("3 findings") and HIGH count when present
  ("3 findings — 2 HIGH"). The HIGH count is omitted when zero so
  there is no "0 HIGH" noise.
- Body lists findings one per line with a ``[H]`` / ``[W]`` / ``[I]``
  marker, capped at ``_BODY_LIMIT`` lines with a
  ``... and N more`` trailer. Avoids overflowing Telegram's
  4096-char cap and keeps the message scannable. Full per-finding
  detail is always available via the CLI ``health`` table — the
  Telegram message is the summons, not the deep dive.
- Body always ends with the date range so the operator knows which
  week is being summarised.

Why a separate file (not a method on ``HealthReport`` or
``HealthCheckService``):

- ``HealthReport`` is a pure DTO; adding rendering logic to it
  would pull the ``Notification`` import into the model layer.
- ``HealthCheckService`` is the producer; conflating "compute
  findings" with "compose the message" obscures both concerns
  and makes the producer harder to test independently.
- Future sinks may want a different rendering shape (per-finding
  emails for compliance archives; one-per-HIGH SMS for paged
  on-call). Keeping the renderer in its own module makes that
  variation a "drop in a sibling function" change.

All text is intentionally English (matches the existing
``Finding.message`` content). When a future PR translates
``Finding.message`` to operator-facing Russian, this renderer's
literals migrate at the same time.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...models.health import Severity
from ...models.notification import Notification

if TYPE_CHECKING:
    from ...models.health import HealthReport

# Severity ranking. Highest wins; ties go to the order traversal
# encounters first (irrelevant — there's only one MAX).
_SEVERITY_RANK: dict[Severity, int] = {
    Severity.INFO: 0,
    Severity.WARNING: 1,
    Severity.HIGH: 2,
}

# Short markers for the per-line body. One char would be ambiguous
# in densely-formatted Telegram messages; three chars (brackets +
# letter) reads cleanly in plain text and survives HTML escaping
# unchanged. Same shape the CLI table uses for severity column
# colours, but text-only.
_SEVERITY_MARKER: dict[Severity, str] = {
    Severity.HIGH: "[H]",
    Severity.WARNING: "[W]",
    Severity.INFO: "[I]",
}

# Cap to keep the body readable in Telegram (4096-char hard cap
# on Bot API messages; on dense findings 10 lines is ~600-900
# chars which leaves comfortable room for the date-range trailer
# and the title). Operator with 25 findings reads the top 10 in
# Telegram and switches to the CLI for the full detail.
_BODY_LIMIT: int = 10


def _max_severity(report: HealthReport) -> Severity:
    """Return the highest severity among the report's findings.

    Precondition: ``report.findings`` is non-empty. Caller
    (``health_report_to_notification``) handles the empty case.
    """
    return max(
        (f.severity for f in report.findings),
        key=lambda s: _SEVERITY_RANK[s],
    )


def _make_title(report: HealthReport) -> str:
    """Compose the title.

    Shape: ``Health check: N finding(s)`` plus an optional
    `` — K HIGH`` suffix when K > 0.
    """
    total = len(report.findings)
    high_count = sum(1 for f in report.findings if f.severity == Severity.HIGH)
    plural = "" if total == 1 else "s"
    title = f"Health check: {total} finding{plural}"
    if high_count > 0:
        title += f" — {high_count} HIGH"
    return title


def _make_body(report: HealthReport) -> str:
    """Compose the body: per-finding lines (capped) + date-range trailer."""
    lines: list[str] = []
    for finding in report.findings[:_BODY_LIMIT]:
        marker = _SEVERITY_MARKER.get(finding.severity, "[?]")
        lines.append(f"{marker} {finding.message}")
    overflow = len(report.findings) - _BODY_LIMIT
    if overflow > 0:
        lines.append(f"... and {overflow} more")
    lines.append("")  # blank separator before trailer
    lines.append(
        f"Date range: {report.date_range.start.isoformat()} → {report.date_range.end.isoformat()}",
    )
    return "\n".join(lines)


def health_report_to_notification(report: HealthReport) -> Notification | None:
    """Fold a ``HealthReport`` into one summary ``Notification``.

    Returns ``None`` for a clean (zero-finding) report — see
    module docstring for the rationale.
    """
    if not report.findings:
        return None
    return Notification(
        severity=_max_severity(report),
        title=_make_title(report),
        body=_make_body(report),
    )


__all__ = ["health_report_to_notification"]
