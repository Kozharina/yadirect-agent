"""Test harness for agent evals.

Wires the real ``Agent`` loop and the real tool registry against a
scripted ``FakeAnthropic`` (no real Claude API) and an in-memory
``DirectService`` fake (no real Yandex.Direct API). Tests assert on
the resulting ``AgentRun`` — what tools the model picked, with what
arguments, and how many iterations / tokens it took.

Why a dedicated harness rather than reusing the unit-test fakes:
unit tests assert on isolated layers (one service, one tool
handler). Evals deliberately exercise the **whole** loop —
agent reasoning + tool dispatch + safety pipeline + service
contract — so the fake has to cover every method any tool might
call. Sharing one fake type across all evals keeps eval files
themselves short.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

# Re-export so tests can ``from .harness import FakeAnthropic, ...``
# rather than reaching into the unit-test conftest. ``__all__``
# below makes the re-export status explicit (CodeQL py/unused-import
# respects ``__all__`` membership; ruff F401 is silenced by the
# noqa). Two signals — one for human readers, one for static
# analysis — saying "yes, importing these is intentional".
from tests.unit.agent.conftest import (
    FakeAnthropic,
    FakeMessage,
    make_message,
    text_block,
    tool_use,
)
from yadirect_agent.agent.loop import Agent, AgentRun
from yadirect_agent.agent.tools import build_default_registry
from yadirect_agent.config import Settings
from yadirect_agent.models.campaigns import Campaign
from yadirect_agent.models.keywords import Keyword, KeywordBid

__all__ = [
    "Agent",
    "AgentRun",
    "EvalResult",
    "FakeAnthropic",
    "FakeDirectService",
    "FakeMessage",
    "make_message",
    "patch_direct_service",
    "run_agent_eval",
    "text_block",
    "tool_use",
    "write_policy",
]

# --------------------------------------------------------------------------
# Unified DirectService fake covering every method a tool handler may call.
# --------------------------------------------------------------------------


@dataclass
class FakeDirectService:
    """In-memory replacement for ``DirectService``.

    Seeded by tests with campaigns / keywords; captures every mutating
    call (``suspend_campaigns`` / ``resume_campaigns`` / ``set_keyword_bids``
    / ``update_campaign_budget``) so assertions can check what the
    agent actually did.
    """

    campaigns: list[Campaign] = field(default_factory=list)
    keywords: list[Keyword] = field(default_factory=list)
    suspend_calls: list[list[int]] = field(default_factory=list)
    resume_calls: list[list[int]] = field(default_factory=list)
    budget_calls: list[tuple[int, int, str]] = field(default_factory=list)
    set_keyword_bids_calls: list[list[KeywordBid]] = field(default_factory=list)

    async def __aenter__(self) -> FakeDirectService:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    async def get_campaigns(
        self,
        ids: list[int] | None = None,
        states: list[str] | None = None,
        types: list[str] | None = None,
        limit: int = 500,
    ) -> list[Campaign]:
        out = list(self.campaigns)
        if ids:
            out = [c for c in out if c.id in ids]
        if states:
            out = [c for c in out if c.state and c.state.value in states]
        return out

    async def get_keywords(
        self,
        adgroup_ids: list[int] | None = None,
        *,
        keyword_ids: list[int] | None = None,
        limit: int = 10_000,
    ) -> list[Keyword]:
        out = list(self.keywords)
        if keyword_ids:
            out = [k for k in out if k.id in keyword_ids]
        if adgroup_ids:
            out = [k for k in out if k.ad_group_id in adgroup_ids]
        return out

    async def suspend_campaigns(self, ids: list[int]) -> dict[str, Any]:
        self.suspend_calls.append(list(ids))
        return {}

    async def resume_campaigns(self, ids: list[int]) -> dict[str, Any]:
        self.resume_calls.append(list(ids))
        return {}

    async def update_campaign_budget(
        self, campaign_id: int, daily_budget_rub: int, mode: str = "STANDARD"
    ) -> dict[str, Any]:
        self.budget_calls.append((campaign_id, daily_budget_rub, mode))
        return {}

    async def set_keyword_bids(self, bids: list[KeywordBid]) -> dict[str, Any]:
        self.set_keyword_bids_calls.append(list(bids))
        return {}


# --------------------------------------------------------------------------
# Patching DirectService at every consumer site.
# --------------------------------------------------------------------------


def patch_direct_service(monkeypatch: pytest.MonkeyPatch, fake: FakeDirectService) -> None:
    """Replace ``DirectService`` at every ``from ... import`` site.

    Each consuming module (``clients.direct``, ``services.campaigns``,
    ``services.bidding``) imported the symbol by name, so the
    standard pydantic / monkeypatch gotcha applies — patching the
    source module isn't enough. Centralised here so eval tests don't
    repeat the three patch lines verbatim.
    """

    def _factory(_settings: Settings) -> FakeDirectService:
        return fake

    monkeypatch.setattr("yadirect_agent.services.campaigns.DirectService", _factory)
    monkeypatch.setattr("yadirect_agent.services.bidding.DirectService", _factory)
    monkeypatch.setattr("yadirect_agent.clients.direct.DirectService", _factory)


# --------------------------------------------------------------------------
# Policy-file helper. ``build_default_registry`` reads the YAML at
# ``settings.agent_policy_path``; missing file → defaults to
# rollout_stage="shadow" (read-only) which would block every
# mutating eval. Most evals call this with autonomy_full.
# --------------------------------------------------------------------------


def write_policy(
    path: Path,
    *,
    account_daily_budget_cap_rub: int = 50_000,
    rollout_stage: str = "autonomy_full",
    extra: dict[str, Any] | None = None,
) -> None:
    """Write a minimal ``agent_policy.yml`` for an eval.

    Defaults are permissive (``autonomy_full`` stage,
    50_000 RUB cap) so happy-path evals can mutate without
    seeing every action blocked at the rollout-stage gate.
    Reject-path evals override ``account_daily_budget_cap_rub``
    or pass ``extra`` to tune the policy further.
    """
    import yaml

    payload: dict[str, Any] = {
        "account_daily_budget_cap_rub": account_daily_budget_cap_rub,
        "rollout_stage": rollout_stage,
    }
    if extra:
        payload.update(extra)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")


# --------------------------------------------------------------------------
# EvalResult — surfaces the metrics every eval pins for regression.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class EvalResult:
    """Metrics from a single eval run.

    Tests construct this from the ``AgentRun`` so the metrics live on
    one line of the assertion block, not scattered across many.
    A future ``--eval-report`` pytest plugin can collect these into
    a JSONL summary at end-of-run; for now they're just visible in
    test output via an ``assert`` failure message.
    """

    name: str
    iterations: int
    input_tokens: int
    output_tokens: int
    tool_names: tuple[str, ...]

    @classmethod
    def from_run(cls, name: str, run: AgentRun) -> EvalResult:
        return cls(
            name=name,
            iterations=run.iterations,
            input_tokens=run.input_tokens,
            output_tokens=run.output_tokens,
            tool_names=tuple(c.name for c in run.tool_calls),
        )


# --------------------------------------------------------------------------
# The runner itself. Thin convenience over ``Agent(...).run(task)`` so
# eval files don't repeat the wiring.
# --------------------------------------------------------------------------


async def run_agent_eval(
    *,
    settings: Settings,
    fake_anthropic: FakeAnthropic,
    user_task: str,
    max_iterations: int = 10,
) -> AgentRun:
    """Build the real registry against the patched ``DirectService``
    fake, wrap it with the real ``Agent``, and run ``user_task`` to
    completion.

    The caller is responsible for having patched ``DirectService``
    via ``patch_direct_service`` before calling this — the patch
    must be active at registry-build time so the safety-trio
    factories pick up the fake.
    """

    registry = build_default_registry(settings)
    agent = Agent(
        settings,
        registry,
        client=fake_anthropic,  # type: ignore[arg-type]  # FakeAnthropic shape matches
        max_iterations=max_iterations,
    )
    return await agent.run(user_task)
