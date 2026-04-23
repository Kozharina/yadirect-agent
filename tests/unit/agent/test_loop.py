"""Tests for the Agent loop.

We inject `FakeAnthropic` so no real Claude API is contacted. Scenarios:

- Happy path: model immediately responds with end_turn + text.
- Tool use: assistant asks for a tool, we dispatch, feed tool_result back,
  assistant ends. Verify the tool handler ran, the final_text is captured,
  and accumulated token counts reflect both turns.
- Parallel vs serial: assistant emits two tool_use blocks in one turn, one
  read and one write. The write must complete after the read starts — we
  check that via an asyncio.Event rendezvous (asserts ordering without
  relying on wall-clock sleeps).
- Unknown tool: dispatcher fills a tool_result with is_error=True, agent
  continues, model then ends.
- Pydantic-invalid arguments: dispatcher returns is_error=True, not an
  exception.
- Handler raises: same — surfaced as tool_result, not bubbled up.
- Repetition guard: same (name, args) fired 5 times in a row aborts.
- max_iterations exceeded: loop raises cleanly.
- Unexpected stop_reason (e.g. max_tokens): returns a terminal AgentRun
  without raising.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest
from pydantic import BaseModel

from yadirect_agent.agent.loop import (
    Agent,
    MaxIterationsExceededError,
    RepetitionDetectedError,
    RepetitionDetector,
)
from yadirect_agent.agent.tools import Tool, ToolContext, ToolRegistry
from yadirect_agent.config import Settings

from .conftest import FakeAnthropic, make_message, text_block, tool_use

# --------------------------------------------------------------------------
# A minimal tool registry for loop tests — no HTTP, deterministic outputs.
# --------------------------------------------------------------------------


class _EchoInput(BaseModel):
    value: str = "ping"


class _WriteInput(BaseModel):
    id: int


@dataclass
class _RecordingRegistry:
    invocations: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def build(
        self,
        *,
        read_handler: Any = None,
        write_handler: Any = None,
    ) -> ToolRegistry:
        reg = ToolRegistry()

        async def default_read(inp: BaseModel, _ctx: ToolContext) -> Any:
            self.invocations.append(("read", inp.model_dump()))
            return {"ok": True, "value": inp.model_dump().get("value")}

        async def default_write(inp: BaseModel, _ctx: ToolContext) -> Any:
            self.invocations.append(("write", inp.model_dump()))
            return {"ok": True, "id": inp.model_dump().get("id")}

        reg.add(
            Tool(
                name="read_tool",
                description="read stuff",
                input_model=_EchoInput,
                is_write=False,
                handler=read_handler or default_read,
            )
        )
        reg.add(
            Tool(
                name="write_tool",
                description="write stuff",
                input_model=_WriteInput,
                is_write=True,
                handler=write_handler or default_write,
            )
        )
        return reg


# --------------------------------------------------------------------------
# RepetitionDetector unit tests — cheap to cover directly.
# --------------------------------------------------------------------------


class TestRepetitionDetector:
    def test_same_args_five_times_raises(self) -> None:
        det = RepetitionDetector(max_consecutive=5)
        for _ in range(4):
            det.observe("t", {"a": 1})
        with pytest.raises(RepetitionDetectedError):
            det.observe("t", {"a": 1})

    def test_different_args_reset_the_counter(self) -> None:
        det = RepetitionDetector(max_consecutive=3)
        det.observe("t", {"a": 1})  # count=1
        det.observe("t", {"a": 1})  # count=2
        # Different argument resets the run.
        det.observe("t", {"a": 2})  # count=1
        det.observe("t", {"a": 2})  # count=2
        with pytest.raises(RepetitionDetectedError):
            det.observe("t", {"a": 2})  # count=3 → raise

    def test_different_names_are_distinct(self) -> None:
        det = RepetitionDetector(max_consecutive=3)
        for _ in range(5):
            det.observe("t1", {"a": 1})
            det.observe("t2", {"a": 1})
        # Never two-of-the-same in a row, so no raise.

    def test_requires_sensible_limit(self) -> None:
        with pytest.raises(ValueError, match=">= 2"):
            RepetitionDetector(max_consecutive=1)


# --------------------------------------------------------------------------
# Agent loop scenarios.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_end_turn_immediately_returns_text(settings: Settings) -> None:
    client = FakeAnthropic(
        [
            make_message(
                [text_block("all done")],
                "end_turn",
                input_tokens=11,
                output_tokens=3,
            ),
        ]
    )
    reg = _RecordingRegistry().build()

    agent = Agent(settings, reg, client=client)
    run = await agent.run("hi")

    assert run.final_text == "all done"
    assert run.tool_calls == []
    assert run.iterations == 1
    assert run.input_tokens == 11
    assert run.output_tokens == 3
    assert run.stop_reason == "end_turn"
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_single_tool_roundtrip(settings: Settings) -> None:
    recording = _RecordingRegistry()
    reg = recording.build()

    client = FakeAnthropic(
        [
            make_message(
                [tool_use("read_tool", {"value": "x"}, id="tu_a")],
                "tool_use",
                input_tokens=10,
                output_tokens=5,
            ),
            make_message(
                [text_block("finished reading")],
                "end_turn",
                input_tokens=12,
                output_tokens=4,
            ),
        ]
    )

    agent = Agent(settings, reg, client=client)
    run = await agent.run("read something")

    assert run.final_text == "finished reading"
    assert run.iterations == 2
    assert run.input_tokens == 22
    assert run.output_tokens == 9
    assert [c.name for c in run.tool_calls] == ["read_tool"]
    assert run.tool_calls[0].ok is True
    assert recording.invocations == [("read", {"value": "x"})]

    # Second create call receives the tool_result in the messages list.
    second_messages = client.calls[1]["messages"]
    assert second_messages[-1]["role"] == "user"
    assert second_messages[-1]["content"][0]["type"] == "tool_result"
    assert second_messages[-1]["content"][0]["tool_use_id"] == "tu_a"
    assert second_messages[-1]["content"][0]["is_error"] is False


@pytest.mark.asyncio
async def test_reads_run_in_parallel_writes_run_serially(settings: Settings) -> None:
    # Two reads should be able to overlap; a write must not start until its
    # prior reads are awaited. We verify ordering via an asyncio.Event.
    read_started = asyncio.Event()
    write_cleared_to_run = asyncio.Event()

    async def slow_read(inp: BaseModel, _ctx: ToolContext) -> Any:
        read_started.set()
        # Wait until the test signals write can proceed — proves the write
        # handler hasn't started before the read was dispatched.
        await asyncio.wait_for(write_cleared_to_run.wait(), timeout=1.0)
        return {"read": inp.model_dump()}

    async def writing(inp: BaseModel, _ctx: ToolContext) -> Any:
        # By contract, the write runs only after the parallel read block
        # finishes. Once this starts, the read has been awaited already.
        assert read_started.is_set()
        return {"wrote": inp.model_dump()}

    recording = _RecordingRegistry()
    reg = recording.build(read_handler=slow_read, write_handler=writing)

    client = FakeAnthropic(
        [
            make_message(
                [
                    tool_use("read_tool", {"value": "r1"}, id="r1"),
                    tool_use("write_tool", {"id": 42}, id="w1"),
                ],
                "tool_use",
            ),
            make_message([text_block("ok")], "end_turn"),
        ]
    )

    agent = Agent(settings, reg, client=client)

    # Let the read progress as soon as it starts.
    async def release() -> None:
        await read_started.wait()
        write_cleared_to_run.set()

    _, run = await asyncio.gather(release(), agent.run("mixed turn"))

    assert [c.name for c in run.tool_calls] == ["read_tool", "write_tool"]
    assert all(c.ok for c in run.tool_calls)


@pytest.mark.asyncio
async def test_unknown_tool_yields_error_result(settings: Settings) -> None:
    reg = _RecordingRegistry().build()
    client = FakeAnthropic(
        [
            make_message(
                [tool_use("does_not_exist", {"anything": True}, id="tu_x")],
                "tool_use",
            ),
            make_message([text_block("recovered")], "end_turn"),
        ]
    )

    agent = Agent(settings, reg, client=client)
    run = await agent.run("pick a bad tool")

    assert run.final_text == "recovered"
    assert len(run.tool_calls) == 1
    assert run.tool_calls[0].ok is False
    assert run.tool_calls[0].error is not None

    # The tool_result the assistant sees must be marked is_error.
    second_messages = client.calls[1]["messages"]
    tool_result = second_messages[-1]["content"][0]
    assert tool_result["is_error"] is True


@pytest.mark.asyncio
async def test_invalid_tool_input_is_surfaced_as_error(settings: Settings) -> None:
    reg = _RecordingRegistry().build()
    client = FakeAnthropic(
        [
            # write_tool expects {"id": int}; sending a string triggers pydantic.
            make_message(
                [tool_use("write_tool", {"id": "not-an-int"}, id="w1")],
                "tool_use",
            ),
            make_message([text_block("noted")], "end_turn"),
        ]
    )

    agent = Agent(settings, reg, client=client)
    run = await agent.run("mistype")

    assert run.tool_calls[0].ok is False
    assert "input validation failed" in (run.tool_calls[0].error or "")


@pytest.mark.asyncio
async def test_handler_exception_is_surfaced_as_error(settings: Settings) -> None:
    async def boom(_inp: BaseModel, _ctx: ToolContext) -> Any:
        msg = "handler boom"
        raise RuntimeError(msg)

    reg = _RecordingRegistry().build(read_handler=boom)
    client = FakeAnthropic(
        [
            make_message([tool_use("read_tool", {"value": "x"}, id="r1")], "tool_use"),
            make_message([text_block("observed")], "end_turn"),
        ]
    )

    agent = Agent(settings, reg, client=client)
    run = await agent.run("break the tool")

    assert run.tool_calls[0].ok is False
    assert "RuntimeError" in (run.tool_calls[0].error or "")
    assert "handler boom" in (run.tool_calls[0].error or "")


@pytest.mark.asyncio
async def test_repetition_of_same_tool_aborts(settings: Settings) -> None:
    reg = _RecordingRegistry().build()
    # Five identical tool_use turns, each followed by the same tool_use.
    client = FakeAnthropic(
        [
            make_message([tool_use("read_tool", {"value": "x"}, id=f"r{i}")], "tool_use")
            for i in range(5)
        ]
    )

    agent = Agent(settings, reg, client=client, repetition_limit=5)
    with pytest.raises(RepetitionDetectedError):
        await agent.run("infinite loop")


@pytest.mark.asyncio
async def test_max_iterations_exceeded_raises(settings: Settings) -> None:
    reg = _RecordingRegistry().build()
    # Each turn uses a *different* argument so the repetition detector does
    # not fire, but the model never ends.
    client = FakeAnthropic(
        [
            make_message([tool_use("read_tool", {"value": f"v{i}"}, id=f"r{i}")], "tool_use")
            for i in range(5)
        ]
    )

    agent = Agent(settings, reg, client=client, max_iterations=3)
    with pytest.raises(MaxIterationsExceededError):
        await agent.run("runaway")


@pytest.mark.asyncio
async def test_unexpected_stop_reason_returns_terminal_run(
    settings: Settings,
) -> None:
    reg = _RecordingRegistry().build()
    client = FakeAnthropic(
        [
            make_message([text_block("I was about to say")], "max_tokens"),
        ]
    )

    agent = Agent(settings, reg, client=client)
    run = await agent.run("hit ceiling")

    assert run.stop_reason == "max_tokens"
    assert run.final_text.startswith("I was about to say")
    assert run.iterations == 1


@pytest.mark.asyncio
async def test_tokens_accumulate_across_turns(settings: Settings) -> None:
    reg = _RecordingRegistry().build()
    client = FakeAnthropic(
        [
            make_message(
                [tool_use("read_tool", {"value": "x"}, id="r1")],
                "tool_use",
                input_tokens=5,
                output_tokens=2,
            ),
            make_message(
                [text_block("done")],
                "end_turn",
                input_tokens=7,
                output_tokens=3,
            ),
        ]
    )

    agent = Agent(settings, reg, client=client)
    run = await agent.run("count tokens")

    assert run.input_tokens == 12
    assert run.output_tokens == 5


@pytest.mark.asyncio
async def test_registry_schemas_sent_to_model(settings: Settings) -> None:
    reg = _RecordingRegistry().build()
    client = FakeAnthropic([make_message([text_block("ok")], "end_turn")])

    agent = Agent(settings, reg, client=client)
    await agent.run("nothing")

    sent_tools = client.calls[0]["tools"]
    names = {t["name"] for t in sent_tools}
    assert names == {"read_tool", "write_tool"}
