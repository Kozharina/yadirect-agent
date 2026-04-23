"""Smoke tests for the system prompt.

We don't compare against a golden string (that would be a brittle diff trap).
We assert the prompt's key commitments survive edits — role, sandbox
emphasis, non-negotiable rules.
"""

from __future__ import annotations

from yadirect_agent.agent.prompts import SYSTEM_PROMPT


def test_system_prompt_is_non_empty_and_bounded() -> None:
    assert SYSTEM_PROMPT
    # Keep it compact — the prompt gets re-sent on every agent iteration.
    assert len(SYSTEM_PROMPT) < 4000


def test_system_prompt_states_sandbox_default() -> None:
    assert "sandbox" in SYSTEM_PROMPT.lower()


def test_system_prompt_scopes_to_ads() -> None:
    # Must forbid touching billing / sharing / account settings.
    lower = SYSTEM_PROMPT.lower()
    assert "billing" in lower or "account settings" in lower
    assert "sharing" in lower


def test_system_prompt_names_direct() -> None:
    assert "yandex.direct" in SYSTEM_PROMPT.lower()
