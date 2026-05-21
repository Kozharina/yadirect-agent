"""Tests for ``services/notify/render.py`` — HealthReport → Notification.

The render layer translates a ``HealthReport`` (the output of
``HealthCheckService.run_account_check``) into ONE summary
``Notification`` suitable for the Dispatcher to fan out.

One summary, not one-per-finding, because:

- Operator inbox protection. A health-check that surfaces 20
  findings would otherwise produce 20 Telegram pings; the operator
  silences the channel within a week.
- Cognitive load. The "what should I look at first" decision lives
  in the summary itself (max severity, HIGH count in the title).
  Per-finding pings would force the operator to mentally re-aggregate.
- Channel-medium parity. Slack, Email, future SMS all share the
  same "one alert per check" cadence; rendering the same summary
  through different sinks keeps the operator's mental model uniform.

Per-finding detail (line-by-line in the body) is still preserved —
the operator can read the breakdown directly in the Telegram message
without context-switching to the CLI.
"""

from __future__ import annotations

from datetime import date

from yadirect_agent.models.health import Finding, HealthReport, Severity
from yadirect_agent.models.metrika import DateRange
from yadirect_agent.services.notify.render import health_report_to_notification

_WEEK = DateRange(start=date(2026, 5, 14), end=date(2026, 5, 20))


def _finding(
    *,
    severity: Severity = Severity.WARNING,
    rule_id: str = "low_ctr",
    message: str = "campaign 'brand' has low CTR 0.10% (1 click / 1000)",
    impact: float | None = None,
    campaign_id: int | None = 42,
    campaign_name: str | None = "brand",
) -> Finding:
    return Finding(
        rule_id=rule_id,
        severity=severity,
        campaign_id=campaign_id,
        campaign_name=campaign_name,
        message=message,
        estimated_impact_rub=impact,
    )


class TestEmptyReport:
    def test_no_findings_returns_none(self) -> None:
        # A clean health report should NOT trigger a notification.
        # Operator who runs the daily check and gets "no issues"
        # doesn't need a "no news" ping every morning — they would
        # learn to ignore the channel.
        report = HealthReport(date_range=_WEEK, findings=[])
        assert health_report_to_notification(report) is None


class TestSeverityAggregation:
    def test_severity_is_max_when_high_present(self) -> None:
        # One HIGH among warnings → notification severity = HIGH.
        # The operator scans severity first to decide urgency;
        # picking max preserves the most-actionable signal.
        report = HealthReport(
            date_range=_WEEK,
            findings=[
                _finding(severity=Severity.WARNING),
                _finding(severity=Severity.HIGH),
                _finding(severity=Severity.INFO),
            ],
        )
        n = health_report_to_notification(report)
        assert n is not None
        assert n.severity == Severity.HIGH

    def test_severity_is_warning_when_no_high(self) -> None:
        report = HealthReport(
            date_range=_WEEK,
            findings=[
                _finding(severity=Severity.WARNING),
                _finding(severity=Severity.INFO),
            ],
        )
        n = health_report_to_notification(report)
        assert n is not None
        assert n.severity == Severity.WARNING

    def test_severity_is_info_when_only_info(self) -> None:
        report = HealthReport(
            date_range=_WEEK,
            findings=[_finding(severity=Severity.INFO)],
        )
        n = health_report_to_notification(report)
        assert n is not None
        assert n.severity == Severity.INFO


class TestTitle:
    def test_title_includes_total_finding_count(self) -> None:
        report = HealthReport(
            date_range=_WEEK,
            findings=[
                _finding(severity=Severity.WARNING),
                _finding(severity=Severity.WARNING),
                _finding(severity=Severity.INFO),
            ],
        )
        n = health_report_to_notification(report)
        assert n is not None
        # "3 findings" so the operator sees scale at-a-glance.
        assert "3" in n.title

    def test_title_calls_out_high_count_when_high_present(self) -> None:
        # HIGH count goes in the title (not just the body) so the
        # Telegram preview shows urgency before the operator
        # expands the message.
        report = HealthReport(
            date_range=_WEEK,
            findings=[
                _finding(severity=Severity.HIGH),
                _finding(severity=Severity.HIGH),
                _finding(severity=Severity.WARNING),
            ],
        )
        n = health_report_to_notification(report)
        assert n is not None
        assert "2" in n.title
        assert "HIGH" in n.title.upper()

    def test_title_omits_high_count_when_no_high(self) -> None:
        # No HIGH → no "0 HIGH" noise in the title.
        report = HealthReport(
            date_range=_WEEK,
            findings=[_finding(severity=Severity.WARNING)],
        )
        n = health_report_to_notification(report)
        assert n is not None
        assert "HIGH" not in n.title.upper()


class TestBody:
    def test_body_contains_each_finding_message(self) -> None:
        # Per-finding detail lives in the body — operator reads it
        # without leaving Telegram. Each line is the rule's emitted
        # message, with a severity marker prefix for scan-ability.
        report = HealthReport(
            date_range=_WEEK,
            findings=[
                _finding(rule_id="burning_campaign", message="campaign 'brand' burned 2400 RUB"),
                _finding(rule_id="low_ctr", message="campaign 'search' has low CTR 0.2%"),
            ],
        )
        n = health_report_to_notification(report)
        assert n is not None
        assert "campaign 'brand' burned 2400 RUB" in n.body
        assert "campaign 'search' has low CTR 0.2%" in n.body

    def test_body_includes_severity_marker_per_line(self) -> None:
        # The body should let the operator scan severity per-line
        # without parsing the rule_id. Marker comes first on each
        # line, mirroring the CLI table convention. Markers are
        # short and unambiguous: [H] HIGH, [W] WARNING, [I] INFO.
        report = HealthReport(
            date_range=_WEEK,
            findings=[
                _finding(severity=Severity.HIGH, message="urgent thing"),
                _finding(severity=Severity.WARNING, message="warn thing"),
                _finding(severity=Severity.INFO, message="info thing"),
            ],
        )
        n = health_report_to_notification(report)
        assert n is not None
        lines = n.body.splitlines()
        # First three lines map to the three findings.
        assert any("[H]" in line and "urgent thing" in line for line in lines)
        assert any("[W]" in line and "warn thing" in line for line in lines)
        assert any("[I]" in line and "info thing" in line for line in lines)

    def test_body_truncates_long_lists(self) -> None:
        # A report with 25 findings shouldn't paste 25 lines into
        # a Telegram message — the medium has a 4096-char hard cap
        # and the operator can't actually read all 25 in-channel.
        # Cap at 10 lines + "... and N more" trailer; full detail
        # is available via the CLI ``health`` table.
        report = HealthReport(
            date_range=_WEEK,
            findings=[
                _finding(
                    rule_id=f"rule_{i}",
                    message=f"finding number {i}",
                    severity=Severity.WARNING,
                )
                for i in range(25)
            ],
        )
        n = health_report_to_notification(report)
        assert n is not None
        # First 10 are present.
        assert "finding number 0" in n.body
        assert "finding number 9" in n.body
        # 10th+ index is dropped from the body lines and replaced
        # by a trailer indicating how many were truncated.
        assert "finding number 10" not in n.body
        assert "15 more" in n.body  # 25 total - 10 shown = 15 trimmed

    def test_body_includes_date_range(self) -> None:
        # The operator must know WHICH week these findings cover
        # — Monday's check on last week's data vs Friday's on
        # this week's data both produce "3 findings" but the
        # urgency is different.
        report = HealthReport(
            date_range=_WEEK,
            findings=[_finding()],
        )
        n = health_report_to_notification(report)
        assert n is not None
        assert "2026-05-14" in n.body
        assert "2026-05-20" in n.body
