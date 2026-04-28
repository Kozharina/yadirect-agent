"""Account-health DTOs (M15.5.1).

The output of a rule-based account health check. Designed so the
result can be:

- Pretty-printed in a CLI table (`yadirect-agent health`).
- Returned through an MCP tool (M15.5.x) as structured JSON.
- Fed into the agent's context as a tool result so the LLM mode
  can reason on top of the rule-based pass.

Design choices:

- Frozen dataclasses, not pydantic. These are produced internally
  by `HealthCheckService`, never deserialised from the wire. Frozen
  catches "bump severity for the demo" anti-patterns at the type
  level.
- ``Severity`` is a small ordered enum. Each rule produces findings
  at one of three levels; the operator-visible CLI sorts by it.
- ``estimated_impact_rub`` is optional but encouraged: a finding
  worth flagging usually has a quantifiable cost. None means
  "we know there's an issue but can't quantify it cheaply" (e.g.
  "campaign with broken targeting" — real impact unknown until
  fixed).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum

from .metrika import DateRange


class Severity(StrEnum):
    """How urgent the finding is.

    Stable for log/audit aggregation: a downstream watcher counting
    ``severity:high`` events shouldn't break when we add a new rule.
    """

    INFO = "info"
    WARNING = "warning"
    HIGH = "high"


@dataclass(frozen=True)
class Finding:
    """One concrete problem the agent found.

    Five fields, deliberately small:

    - ``rule_id`` — short stable identifier (``"burning_campaign"``,
      ``"high_cpa"``). Used by the operator to filter / suppress
      specific rules in CLI output and by future reporting (M12)
      to track which rules fire most often.
    - ``severity`` — how loud to be.
    - ``subject`` — what the finding is about (campaign id +
      human-readable name). For non-campaign findings (account-level
      ones added later) ``campaign_id`` will be None.
    - ``message`` — a one-line, operator-facing description in
      English. NOT a free-form essay; that's M12's job. This is what
      shows up in the CLI table row and goes into audit.
    - ``estimated_impact_rub`` — quantified loss / saving in RUB
      where possible.
    """

    rule_id: str
    severity: Severity
    campaign_id: int | None
    campaign_name: str | None
    message: str
    estimated_impact_rub: float | None = None


@dataclass(frozen=True)
class HealthReport:
    """The output of one ``HealthCheckService.run_account_check`` call.

    Carries the date range the check ran over (so the consumer can
    contextualise "last 7 days" vs "yesterday") and the list of
    findings.

    Convenience: ``findings_by_severity`` exposes a pre-bucketed view
    so the CLI renderer doesn't loop the list three times.
    """

    date_range: DateRange
    findings: list[Finding] = field(default_factory=list)
    # Generated_at deliberately omitted — the audit log records when
    # the check ran. Adding it here would invite confusion about
    # which timestamp is canonical.

    @property
    def has_findings(self) -> bool:
        return bool(self.findings)

    def findings_by_severity(self, severity: Severity) -> list[Finding]:
        return [f for f in self.findings if f.severity == severity]


def default_window(days: int = 7) -> DateRange:
    """Build a ``DateRange`` covering the last N days, ending yesterday.

    Yesterday is the end-anchor (not today) because Metrika data for
    the in-flight day is incomplete and lags by a few hours; rule
    decisions on partial data tend to false-positive.
    """
    from datetime import timedelta

    today = date.today()
    end = today - timedelta(days=1)
    start = end - timedelta(days=days - 1)
    return DateRange(start=start, end=end)
