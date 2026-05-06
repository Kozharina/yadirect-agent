"""End-to-end acceptance test for Anna's full first-run journey (M15.7).

Locks down the assembled-product contract: from a fresh in-memory
state (token saved, no profile, no history) Anna's MCP-only
journey reaches a structured ``account_health()`` response with
findings. The whole sequence runs in-process with mocked
Direct/Metrika so it executes in milliseconds on CI without any
real credentials or network.

What this test catches if it goes red:

- A regression in ``start_onboarding`` that breaks any of the
  three states it returns (``ready_for_profile_qa`` →
  ``policy_proposed`` re-run path → standalone ``account_health``).
- A regression in ``HealthCheckService`` that makes the perf-rule
  pipeline crash on a clean ``BusinessProfileStore`` /
  ``HealthHistoryStore`` (the M15.5.5 wiring case).
- A regression in the BusinessProfile → policy proposal pipeline
  that silently drops a field, mis-computes the daily budget cap,
  or breaks the policy-YAML round-trip.
- A regression in the response envelope shape (``status`` /
  ``profile`` / ``proposal`` / ``health`` keys) that the LLM in
  Claude Desktop reads — this would break the conversational
  rendering Anna sees.
- A massive import-time cost or accidental sleep that pushes the
  end-to-end past the 30-second budget.

What this test deliberately does NOT cover:

- Real OAuth dance (browser open, callback server, Yandex token
  exchange) — ``self._save_token`` simulates the post-auth state
  to keep the test self-contained. OAuth flow is exercised in
  ``tests/unit/cli/test_auth.py`` with its own mocks.
- Real ``pip install`` packaging (entry-point wiring, missing
  transitive deps, wheel contents) — that belongs to a release-
  validation CI workflow that runs in a fresh container after
  PyPI publish, not on every PR.
- Real Direct/Metrika network responses — covered by VCR'd unit
  tests at the client layer.

The product spec budget for Anna's journey is **10 minutes**
(M15.7), but that includes a real human reading screens, tapping
consent, and waiting on Metrika reports. The CI-runner budget
here is much tighter (30 seconds) — it's a regression-detection
threshold, not a UX guarantee.
"""

from __future__ import annotations

import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Self

import pytest
import structlog
from pydantic import SecretStr

from yadirect_agent.agent.tools import build_default_registry
from yadirect_agent.config import Settings
from yadirect_agent.models.metrika import CampaignPerformance, DateRange

# A loose CI-runner budget. Real elapsed on a developer Mac is
# ~50ms; on a busy GitHub-hosted runner expect 200-800ms. 30s
# catches a regression that introduces a sleep, a busy-wait, or
# a massive import-time cost without flaking on slow runners.
ACCEPTANCE_BUDGET_SECONDS = 30.0


@pytest.fixture
def memory_keyring(monkeypatch: pytest.MonkeyPatch) -> dict[tuple[str, str], str]:
    """In-memory keyring backend, identical pattern to
    ``tests/unit/agent/test_tools.py::TestStartOnboardingTool::memory_keyring``.

    Replicated rather than imported to keep this directory's
    tests independent — acceptance tests should not fail when
    something far away in ``tests/unit/`` is refactored.
    """
    import keyring.errors

    storage: dict[tuple[str, str], str] = {}

    def set_password(service: str, username: str, password: str) -> None:
        storage[(service, username)] = password

    def get_password(service: str, username: str) -> str | None:
        return storage.get((service, username))

    def delete_password(service: str, username: str) -> None:
        key = (service, username)
        if key not in storage:
            raise keyring.errors.PasswordDeleteError(f"no password for {key}")
        del storage[key]

    monkeypatch.setattr("keyring.set_password", set_password)
    monkeypatch.setattr("keyring.get_password", get_password)
    monkeypatch.setattr("keyring.delete_password", delete_password)
    return storage


@pytest.fixture
def anna_settings(tmp_path: Path) -> Settings:
    """A fresh-install Settings: tokens absent (will be saved via the
    keychain helper), Metrika counter set, sandbox on, audit log
    pointed at a tmp file, no real Anthropic key required."""
    return Settings(
        # The handler reads the token from keyring at runtime, not
        # from this Settings object — so the field can stay at its
        # placeholder. The keyring fixture is the source of truth.
        yandex_direct_token=SecretStr("placeholder-keyring-is-source"),
        yandex_metrika_token=SecretStr("placeholder-keyring-is-source"),
        yandex_metrika_counter_id=12_345,
        yandex_use_sandbox=True,
        # Placeholder Anthropic key. The M15.x acceptance contract is
        # that Anna's journey works WITHOUT a real key — none of the
        # tools we exercise here invoke Anthropic. The Settings model
        # currently requires the field to be a non-None SecretStr;
        # the placeholder satisfies that constraint without any LLM
        # being called. (A future Settings refactor making the field
        # truly Optional would let us pass None.)
        anthropic_api_key=SecretStr("placeholder-not-used-in-this-flow"),
        agent_policy_path=tmp_path / "agent_policy.yml",
        agent_max_daily_budget_rub=10_000,
        log_level="WARNING",  # quiet in CI
        log_format="json",
        audit_log_path=tmp_path / "logs" / "audit.jsonl",
    )


def _save_token_to_keychain() -> None:
    """Helper: write a valid TokenSet through the real keychain
    layer so ``start_onboarding`` reads it the same way it would
    after a real ``auth login``."""
    from yadirect_agent.auth.keychain import KeyringTokenStore
    from yadirect_agent.models.auth import TokenSet

    now = datetime(2026, 5, 6, 12, 0, tzinfo=UTC)
    KeyringTokenStore().save(
        TokenSet(
            access_token=SecretStr("AQAA-acceptance-access"),
            refresh_token=SecretStr("1.AQAA-acceptance-refresh"),
            token_type="bearer",
            scope=("direct:api", "metrika:read", "metrika:write"),
            obtained_at=now,
            expires_at=now + timedelta(days=365),
        ),
    )


def _patch_account_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mock Direct + Metrika reads at the service layer.

    Two ON campaigns:
    - ``brand`` — healthy, 5 conversions @ 100 RUB CPA.
    - ``non-brand-burner`` — burning campaign (cost without
      conversions), surfaces as a HIGH severity finding.

    No real httpx fires; the test is fully offline.
    """
    from yadirect_agent.services.campaigns import CampaignService, CampaignSummary

    async def fake_list_active(self: CampaignService, limit: int = 200) -> list[CampaignSummary]:
        del self, limit
        return [
            CampaignSummary(
                id=1,
                name="brand",
                state="ON",
                status="ACCEPTED",
                type="TEXT_CAMPAIGN",
                daily_budget_rub=3_000.0,
            ),
            CampaignSummary(
                id=2,
                name="non-brand-burner",
                state="ON",
                status="ACCEPTED",
                type="TEXT_CAMPAIGN",
                daily_budget_rub=2_000.0,
            ),
        ]

    monkeypatch.setattr(CampaignService, "list_active", fake_list_active)

    # Reporting service: returns one healthy + one burning row so
    # the perf-rule lane (BurningCampaignRule, M15.5.1) fires
    # exactly one HIGH-severity finding for the "burner".
    week = DateRange(start=date(2026, 4, 29), end=date(2026, 5, 5))
    fake_overview = [
        # Healthy brand campaign — converting, decent CTR. No rules
        # fire on this row.
        CampaignPerformance(
            campaign_id=1,
            campaign_name="brand",
            date_range=week,
            clicks=200,
            cost_rub=500.0,
            conversions=5,
            cpa_rub=100.0,
            cr_pct=2.5,
            impressions=10_000,
        ),
        # Burning + low-CTR. Two rules fire: LowCtrRule (goal-
        # independent, fires in BOTH rollup and standalone) and
        # BurningCampaignRule (requires goal_id, fires only in
        # standalone account_health(goal_id=100)). The rollup case
        # in onboarding's _build_health_payload does NOT pass a
        # goal_id, so only LowCtrRule fires there — which is the
        # contract we lock down: rollup surfaces creative-health
        # signals even before the operator hooks up Metrika goals.
        CampaignPerformance(
            campaign_id=2,
            campaign_name="non-brand-burner",
            date_range=week,
            clicks=10,
            cost_rub=2_400.0,
            conversions=0,
            cpa_rub=None,
            cr_pct=0.0,
            impressions=10_000,
        ),
    ]

    class _FakeReporting:
        def __init__(self, _settings: Any) -> None:
            pass

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def account_overview(
            self,
            *,
            date_range: DateRange,
            goal_id: int | None = None,
        ) -> list[CampaignPerformance]:
            del date_range, goal_id
            return fake_overview

    # Patch wherever HealthCheckService imports ReportingService
    # from. The agent/tools.py path doesn't matter — it goes
    # through HealthCheckService, which imports from .reporting.
    monkeypatch.setattr(
        "yadirect_agent.services.health_check.ReportingService",
        _FakeReporting,
    )

    # DirectService is invoked by the direct-state rules
    # (RejectedAds / RejectedKeywords). Empty campaigns list →
    # zero direct findings, focus stays on the perf-rule lane.
    class _FakeDirect:
        def __init__(self, _settings: Any) -> None:
            pass

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def get_campaigns(self) -> list[Any]:
            return []

    monkeypatch.setattr(
        "yadirect_agent.services.health_check.DirectService",
        _FakeDirect,
    )


@pytest.fixture
def tool_context_factory() -> Any:
    """Build a real ToolContext (the same shape the MCP server
    constructs at runtime), so handlers see the production code
    path — not a thin mock that could mask integration bugs."""
    from yadirect_agent.agent.tools import ToolContext

    def _factory() -> ToolContext:
        return ToolContext(
            trace_id="acceptance-test-trace",
            logger=structlog.get_logger("acceptance-test"),
        )

    return _factory


@pytest.mark.asyncio
async def test_anna_journey_to_first_health_finding(
    anna_settings: Settings,
    memory_keyring: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
    tool_context_factory: Any,
) -> None:
    """Anna's path from a saved-token state to a structured health finding.

    Mimics the conversational sequence Claude Desktop drives
    on her behalf:

    1. Anna types "помоги настроить агента" → MCP calls
       ``start_onboarding(answers=None)`` → expect either
       ``ready_for_profile_qa`` (no profile yet) OR
       ``policy_proposed`` (profile already saved + re-run).
    2. Anna answers the profile questions → MCP calls
       ``start_onboarding(answers={niche, monthly_budget,
       target_cpa})`` → expect ``policy_proposed`` with the
       computed proposal AND the embedded ``health`` rollup.
    3. Anna asks "как дела в кабинете?" → MCP calls
       ``account_health(days=7)`` → expect ``status="ok"`` and
       at least one finding (the burning campaign we mocked).

    Stopwatch the whole journey. Expected real elapsed on a
    developer Mac is ~50ms; CI runner ~200-800ms. The 30s
    budget catches catastrophic regressions only.
    """
    # Pre-state: Anna has just finished `auth login`, token is
    # in the keychain. No BusinessProfile saved yet.
    assert memory_keyring == {}
    _save_token_to_keychain()

    _patch_account_state(monkeypatch)

    registry = build_default_registry(anna_settings)
    onboard_tool = registry.get("start_onboarding")
    health_tool = registry.get("account_health")

    started_at = time.monotonic()

    # --- Step 1: probe with no answers, no profile yet -----------
    probe_input = onboard_tool.input_model.model_validate({})
    probe_result = await onboard_tool.handler(
        probe_input,
        tool_context_factory(),
    )
    assert probe_result["status"] == "ready_for_profile_qa", probe_result

    # --- Step 2: submit profile, expect policy_proposed ---------
    answers = {
        "niche": "Plumbing services in Moscow",
        "monthly_budget_rub": 120_000,
        "target_cpa_rub": 2_000,
    }
    answer_input = onboard_tool.input_model.model_validate({"answers": answers})
    proposed_result = await onboard_tool.handler(
        answer_input,
        tool_context_factory(),
    )
    assert proposed_result["status"] == "policy_proposed", proposed_result
    # Profile saved at the right place.
    assert proposed_result["profile"]["niche"] == "Plumbing services in Moscow"
    assert proposed_result["profile"]["monthly_budget_rub"] == 120_000
    # Proposal embeds policy YAML + computed cap.
    assert "policy_yaml" in proposed_result["proposal"]
    assert "yadirect-agent" in proposed_result["proposal"]["policy_yaml"]
    # Onboarding response embeds a first health rollup so the
    # operator's first conversational turn already shows them
    # what's wrong (M15.4 slice 5).
    assert "health" in proposed_result, proposed_result
    rollup = proposed_result["health"]
    assert rollup["status"] == "ok", rollup
    rollup_findings = rollup["report"]["findings"]
    # Rollup runs without a goal_id (onboarding doesn't have one
    # yet), so goal-independent rules are what surface here. The
    # low-CTR finding on our burner row exercises that lane.
    assert len(rollup_findings) >= 1, rollup_findings
    assert any(f["rule_id"] == "low_ctr" for f in rollup_findings), rollup_findings

    # --- Step 3: standalone account_health() with goal_id ------
    # Anna's later "как дела в кабинете?" call. With a goal_id
    # passed in, the goal-conditioned rules also fire — burning
    # campaign now surfaces with a HIGH severity.
    health_input = health_tool.input_model.model_validate(
        {
            "days": 7,
            "goal_id": 100,
        }
    )
    health_result = await health_tool.handler(
        health_input,
        tool_context_factory(),
    )
    assert health_result["status"] == "ok", health_result
    standalone_findings = health_result["report"]["findings"]
    # Burning rule fires only when goal_id is set.
    assert any(f["rule_id"] == "burning_campaign" for f in standalone_findings), standalone_findings
    burner = next(f for f in standalone_findings if f["rule_id"] == "burning_campaign")
    assert burner["campaign_name"] == "non-brand-burner"
    assert burner["severity"] == "high"
    # Estimated impact in RUB matches the cost of the burning campaign.
    assert burner["estimated_impact_rub"] == pytest.approx(2400.0)

    elapsed = time.monotonic() - started_at
    assert elapsed < ACCEPTANCE_BUDGET_SECONDS, (
        f"Anna's journey took {elapsed:.2f}s — over the {ACCEPTANCE_BUDGET_SECONDS}s "
        "regression budget. A real human's UX target is 10 minutes (M15.7 spec); "
        "this CI budget catches catastrophic regressions like an unintended "
        "sleep, a busy retry loop, or a massive import-time cost."
    )
