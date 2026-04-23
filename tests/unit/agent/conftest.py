"""Helpers for agent tests.

- `FakeAnthropic` replaces `anthropic.AsyncAnthropic`. It yields a scripted
  sequence of Message-like responses and records every `create` call for
  assertions.
- `make_message` builds a Message-like object from content blocks, stop
  reason, and token counts. Uses a plain class so `getattr`-based code paths
  in the loop behave the same as with the SDK's pydantic models.
- `tool_use` / `text_block` are tiny helpers to avoid dict boilerplate in
  test bodies.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

import pytest
import structlog

from yadirect_agent.agent.tools import ToolContext


@dataclass
class _Usage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class FakeMessage:
    content: list[Any]
    stop_reason: str
    usage: _Usage = field(default_factory=_Usage)


def make_message(
    content: list[Any],
    stop_reason: str,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> FakeMessage:
    return FakeMessage(
        content=content,
        stop_reason=stop_reason,
        usage=_Usage(input_tokens=input_tokens, output_tokens=output_tokens),
    )


def tool_use(name: str, input_data: dict[str, Any], *, id: str = "tu_1") -> dict[str, Any]:
    return {"type": "tool_use", "id": id, "name": name, "input": input_data}


def text_block(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


class FakeAnthropic:
    """Stand-in for anthropic.AsyncAnthropic that returns pre-scripted turns.

    Access pattern: `FakeAnthropic(...)` exposes `.messages.create(...)` just
    like the real client. Every call pops the next scripted `FakeMessage`.
    """

    def __init__(self, turns: list[FakeMessage]) -> None:
        self._turns = list(turns)
        self.calls: list[dict[str, Any]] = []
        self.messages = self  # self-reference so `.messages.create` works

    async def create(self, **kwargs: Any) -> FakeMessage:
        # Deep-copy so later mutations of `messages` in the loop don't
        # rewrite this captured snapshot. Tests assert on the exact state
        # that was passed to the model on each turn.
        self.calls.append(copy.deepcopy(kwargs))
        if not self._turns:
            msg = "FakeAnthropic: no more scripted turns"
            raise AssertionError(msg)
        return self._turns.pop(0)


@pytest.fixture
def tool_context() -> ToolContext:
    return ToolContext(
        trace_id="test-trace",
        logger=structlog.get_logger().bind(component="test"),
    )
