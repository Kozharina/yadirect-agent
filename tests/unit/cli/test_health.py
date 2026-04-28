"""Tests for the ``health`` CLI command renderer (M15.5.1).

Two surfaces tested:
- ``render_report_text``: stdout content for empty reports and reports
  with findings; sort order; severity colour markers; footer summary.
- ``render_report_json``: structure stability for downstream tools
  (jq, the agent's tool result envelope).
"""

from __future__ import annotations

import json
from datetime import date

import pytest
from rich.console import Console

from yadirect_agent.cli.health import render_report_json, render_report_text
from yadirect_agent.models.health import Finding, HealthReport, Severity
from yadirect_agent.models.metrika import DateRange

_WEEK = DateRange(start=date(2026, 4, 1), end=date(2026, 4, 7))


def _finding(
    *,
    rule_id: str = "burning_campaign",
    severity: Severity = Severity.HIGH,
    campaign_id: int = 51,
    name: str = "non-brand",
    message: str = "campaign 'non-brand' burned 2400 RUB with 0 conversions",
    impact: float | None = 2400.0,
) -> Finding:
    return Finding(
        rule_id=rule_id,
        severity=severity,
        campaign_id=campaign_id,
        campaign_name=name,
        message=message,
        estimated_impact_rub=impact,
    )


def _capture(report: HealthReport) -> str:
    """Render with a Rich console writing to a StringIO buffer."""
    import io

    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, width=200)
    render_report_text(console, report)
    return buf.getvalue()


class TestRenderReportText:
    def test_empty_report_prints_no_issues(self) -> None:
        report = HealthReport(date_range=_WEEK, findings=[])

        out = _capture(report)

        assert "No issues found" in out
        assert "2026-04-01" in out
        assert "2026-04-07" in out

    def test_single_finding_appears_in_output(self) -> None:
        report = HealthReport(date_range=_WEEK, findings=[_finding()])

        out = _capture(report)

        assert "burning_campaign" in out
        assert "non-brand" in out
        assert "2400" in out  # impact
        assert "high" in out  # severity

    def test_findings_sorted_by_severity_then_impact(self) -> None:
        # HIGH 2400, WARNING 6000, HIGH 600 → in render order:
        #   HIGH (2400), HIGH (600), WARNING (6000)
        # because HIGH severity beats higher impact in WARNING.
        report = HealthReport(
            date_range=_WEEK,
            findings=[
                _finding(rule_id="high_cpa", severity=Severity.WARNING, impact=6000.0),
                _finding(rule_id="burning_campaign", impact=2400.0, campaign_id=51),
                _finding(rule_id="burning_campaign", impact=600.0, campaign_id=73),
            ],
        )

        out = _capture(report)

        # Pull positions of rule_id occurrences in the rendered text
        # to verify ordering.
        pos_2400 = out.find("2400")
        pos_600 = out.find("600", pos_2400 + 1)  # the next 600 after 2400
        pos_6000 = out.find("6000")
        # HIGH 2400 first, then HIGH 600, then WARNING 6000
        assert pos_2400 < pos_600 < pos_6000

    def test_footer_shows_totals_and_severity_counts(self) -> None:
        report = HealthReport(
            date_range=_WEEK,
            findings=[
                _finding(impact=2400.0),
                _finding(rule_id="high_cpa", severity=Severity.WARNING, impact=6000.0),
            ],
        )

        out = _capture(report)

        assert "2 findings" in out
        assert "1 high" in out
        assert "1 warning" in out
        assert "8400" in out  # total impact

    def test_rich_markup_in_campaign_name_is_escaped(self) -> None:
        # Operator-set campaign names are untrusted free text. A name
        # like "[bold red]PWNED[/bold red]" or "[link=file:///etc/passwd]click[/link]"
        # MUST NOT be interpreted by Rich as markup, otherwise it
        # injects styling/links/escape sequences from attacker-controlled
        # data. Bare "[" without a closing tag would also crash the
        # renderer with MarkupError. (auditor M15.5.1 HIGH-1.)
        report = HealthReport(
            date_range=_WEEK,
            findings=[
                _finding(name="[bold red]PWNED[/bold red]"),
            ],
        )

        # Must not raise (no MarkupError) and must contain the literal
        # bracketed text rather than rendered markup.
        out = _capture(report)

        # The literal "[bold red]" should appear in the output —
        # if it didn't, Rich consumed it as markup.
        assert "[bold red]" in out

    def test_rich_markup_in_message_is_escaped(self) -> None:
        # Same protection for the message column (rule-emitted messages
        # also embed campaign_name verbatim via f-string).
        report = HealthReport(
            date_range=_WEEK,
            findings=[
                _finding(
                    message="campaign '[red]injected[/red]' burned 100 RUB",
                ),
            ],
        )

        out = _capture(report)

        assert "[red]injected[/red]" in out

    def test_unbalanced_markup_does_not_crash(self) -> None:
        # An unclosed bracket like "[" would raise MarkupError without
        # escaping — denial-of-service of the health command itself.
        report = HealthReport(
            date_range=_WEEK,
            findings=[_finding(name="campaign-with-[", message="msg-[unclosed")],
        )

        # Must not raise.
        out = _capture(report)

        assert "campaign-with-[" in out

    def test_account_level_finding_renders_without_campaign(self) -> None:
        # No campaign_id (some future rule fires at account level —
        # billing, policy mismatch, etc.). Must render as "(account)"
        # rather than crash on None.
        report = HealthReport(
            date_range=_WEEK,
            findings=[
                _finding(campaign_id=None, name=None, message="account-level issue"),  # type: ignore[arg-type]
            ],
        )

        out = _capture(report)

        assert "account" in out.lower()


class TestRenderReportJson:
    def test_empty_report_has_empty_findings(self) -> None:
        report = HealthReport(date_range=_WEEK, findings=[])

        payload = json.loads(render_report_json(report))

        assert payload["date_range"]["start"] == "2026-04-01"
        assert payload["date_range"]["end"] == "2026-04-07"
        assert payload["findings"] == []

    def test_findings_serialised_with_severity_string(self) -> None:
        report = HealthReport(date_range=_WEEK, findings=[_finding()])

        payload = json.loads(render_report_json(report))

        assert len(payload["findings"]) == 1
        f = payload["findings"][0]
        assert f["rule_id"] == "burning_campaign"
        assert f["severity"] == "high"  # string, not Severity enum repr
        assert f["campaign_id"] == 51
        assert f["estimated_impact_rub"] == pytest.approx(2400.0)

    def test_findings_sorted_in_json_too(self) -> None:
        # Same sort order as text mode so jq pipelines and the
        # CLI table don't disagree on what's first.
        report = HealthReport(
            date_range=_WEEK,
            findings=[
                _finding(rule_id="high_cpa", severity=Severity.WARNING, impact=6000.0),
                _finding(impact=2400.0),
            ],
        )

        payload = json.loads(render_report_json(report))

        assert payload["findings"][0]["severity"] == "high"
        assert payload["findings"][1]["severity"] == "warning"
