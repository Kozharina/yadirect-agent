"""The agent loop: drive a Claude tool-use conversation to completion.

Design choices
--------------
- We keep the loop in ~200 lines. Production-grade features like prompt
  caching, streaming, or vision inputs are *intentionally* absent; they
  can be added without touching the control flow.
- One `Agent` instance = one Settings + one ToolRegistry + one AsyncAnthropic
  client. Stateless across runs.
- Parallel execution of *read-only* tool uses per turn via asyncio.gather;
  *write* tools run serially in declaration order. This keeps the audit
  trail reconstructable — we never want two writes to interleave.
- Argument-aware repetition detector: "same tool name + same arguments N
  times in a row" aborts. Same tool with different args is fine — it's the
  normal loop.
- Errors from tool handlers become tool_result with is_error=true so the
  model can react instead of the whole turn dying. Exceptions that escape
  the handler (network, unserialisable output) still bubble up.

Not implemented (explicit):
- Plan -> confirm -> execute. That's M2 (`agent/safety.py`).
- Persistent audit log. The loop logs via structlog; the JSONL audit sink
  is M2.3.
- Cost calculation. Token counts are carried on AgentRun; rubles-per-token
  conversion is out of scope.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Awaitable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, cast

import anthropic
import structlog
from pydantic import ValidationError as PydanticValidationError

from ..config import Settings
from .prompts import SYSTEM_PROMPT
from .tools import Tool, ToolContext, ToolRegistry

if TYPE_CHECKING:  # pragma: no cover
    from anthropic.types import Message


# --------------------------------------------------------------------------
# Public exceptions.
# --------------------------------------------------------------------------


class AgentLoopError(Exception):
    """Base class for loop-level failures that halt execution."""


class MaxIterationsExceededError(AgentLoopError):
    """The model kept asking for tools past max_iterations. Something's wrong."""


class RepetitionDetectedError(AgentLoopError):
    """The model called the same tool with identical arguments too many times."""


# --------------------------------------------------------------------------
# DTOs surfaced to callers.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolCall:
    """One tool invocation with its arguments and outcome.

    `ok=True` means the handler returned successfully. A handler that raised
    produces `ok=False, error=<str>` — the agent itself gets a structured
    tool_result with is_error=true and can recover.
    """

    name: str
    input: dict[str, Any]
    result: Any | None
    error: str | None
    ok: bool


@dataclass(frozen=True)
class AgentRun:
    """Terminal state of a single agent.run() invocation."""

    trace_id: str
    final_text: str
    tool_calls: list[ToolCall]
    iterations: int
    input_tokens: int
    output_tokens: int
    stop_reason: str


# --------------------------------------------------------------------------
# Narrow protocol for the Anthropic async client.
#
# We don't import concrete response types from `anthropic` beyond what's
# needed; this keeps the dependency surface small and lets tests supply a
# fake without mocking internals.
# --------------------------------------------------------------------------


class _MessagesAPI(Protocol):
    async def create(
        self,
        *,
        model: str,
        system: str,
        tools: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        max_tokens: int,
    ) -> Message: ...


class _AnthropicClient(Protocol):
    messages: _MessagesAPI


# --------------------------------------------------------------------------
# Repetition detector.
# --------------------------------------------------------------------------


class RepetitionDetector:
    """Halts the loop when one (tool, args) pair fires too many times in a row.

    The reference (AlessandroAnnini/agent-loop) uses a small k-of-last-N
    sliding window; we use consecutive-run counting because it's simpler and
    catches the "model is stuck" case just as well.
    """

    def __init__(self, max_consecutive: int = 5) -> None:
        if max_consecutive < 2:
            msg = "max_consecutive must be >= 2"
            raise ValueError(msg)
        self._max = max_consecutive
        self._last_key: str | None = None
        self._count = 0

    def observe(self, tool_name: str, input_data: dict[str, Any]) -> None:
        """Record one tool call; raise RepetitionDetectedError if stuck."""
        key = self._key(tool_name, input_data)
        if key == self._last_key:
            self._count += 1
        else:
            self._last_key = key
            self._count = 1

        if self._count >= self._max:
            msg = (
                f"tool {tool_name!r} called with the same arguments "
                f"{self._count} times in a row; aborting to avoid a stuck loop"
            )
            raise RepetitionDetectedError(msg)

    @staticmethod
    def _key(name: str, args: dict[str, Any]) -> str:
        payload = json.dumps(args, sort_keys=True, default=str).encode("utf-8")
        digest = hashlib.sha1(payload, usedforsecurity=False).hexdigest()[:16]
        return f"{name}:{digest}"


# --------------------------------------------------------------------------
# Agent.
# --------------------------------------------------------------------------


@dataclass
class _Accumulator:
    """Mutable bookkeeping carried through a single run()."""

    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0


class Agent:
    """Drives a tool-use dialog between Claude and our tool registry."""

    def __init__(
        self,
        settings: Settings,
        registry: ToolRegistry,
        *,
        client: _AnthropicClient | None = None,
        max_iterations: int = 20,
        max_tokens_per_call: int = 4096,
        repetition_limit: int = 5,
        system_prompt: str = SYSTEM_PROMPT,
    ) -> None:
        if max_iterations < 1:
            msg = "max_iterations must be >= 1"
            raise ValueError(msg)
        self._settings = settings
        self._registry = registry
        self._client: _AnthropicClient = client or cast(
            _AnthropicClient,
            anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key.get_secret_value()),
        )
        self._max_iterations = max_iterations
        self._max_tokens = max_tokens_per_call
        self._repetition_limit = repetition_limit
        self._system_prompt = system_prompt
        self._logger = structlog.get_logger().bind(component="agent")

    async def run(self, user_message: str) -> AgentRun:
        """Execute the tool-use loop until end_turn or a guard trips."""
        trace_id = uuid.uuid4().hex
        log = self._logger.bind(trace_id=trace_id)
        detector = RepetitionDetector(self._repetition_limit)
        accum = _Accumulator()
        messages: list[dict[str, Any]] = [{"role": "user", "content": user_message}]

        ctx = ToolContext(trace_id=trace_id, logger=log)

        for iteration in range(1, self._max_iterations + 1):
            log = log.bind(iteration=iteration)
            log.info("agent.turn.start")
            response = await self._client.messages.create(
                model=self._settings.anthropic_model,
                system=self._system_prompt,
                tools=self._registry.schemas(),
                messages=messages,
                max_tokens=self._max_tokens,
            )
            self._accumulate_tokens(accum, response)

            messages.append({"role": "assistant", "content": _dump_content(response.content)})

            if response.stop_reason == "end_turn":
                final_text = _extract_text(response.content)
                log.info(
                    "agent.turn.end",
                    iterations=iteration,
                    tool_calls=len(accum.tool_calls),
                )
                return AgentRun(
                    trace_id=trace_id,
                    final_text=final_text,
                    tool_calls=list(accum.tool_calls),
                    iterations=iteration,
                    input_tokens=accum.input_tokens,
                    output_tokens=accum.output_tokens,
                    stop_reason=response.stop_reason,
                )

            if response.stop_reason != "tool_use":
                # max_tokens or refusal — surface as a terminal state.
                final_text = _extract_text(response.content) or ""
                log.warning(
                    "agent.stop.unexpected",
                    stop_reason=response.stop_reason,
                )
                return AgentRun(
                    trace_id=trace_id,
                    final_text=final_text,
                    tool_calls=list(accum.tool_calls),
                    iterations=iteration,
                    input_tokens=accum.input_tokens,
                    output_tokens=accum.output_tokens,
                    stop_reason=str(response.stop_reason or "unknown"),
                )

            tool_result_blocks = await self._execute_tool_uses(
                response.content, detector, ctx, accum
            )
            messages.append({"role": "user", "content": tool_result_blocks})

        msg = f"agent exceeded {self._max_iterations} iterations"
        raise MaxIterationsExceededError(msg)

    # ----------------------------------------------------------------------
    # Internal helpers.
    # ----------------------------------------------------------------------

    async def _execute_tool_uses(
        self,
        content: list[Any],
        detector: RepetitionDetector,
        ctx: ToolContext,
        accum: _Accumulator,
    ) -> list[dict[str, Any]]:
        """Run all tool_use blocks from one assistant turn.

        Reads are awaited in parallel; writes are awaited in declaration order
        to keep the audit trail linear.
        """
        tool_use_blocks = [b for b in content if _block_type(b) == "tool_use"]

        read_awaitables: list[tuple[int, Awaitable[ToolCall]]] = []
        write_awaitables: list[tuple[int, Awaitable[ToolCall]]] = []
        results: list[ToolCall | None] = [None] * len(tool_use_blocks)
        use_ids: list[str] = [""] * len(tool_use_blocks)

        for idx, block in enumerate(tool_use_blocks):
            name = _block_attr(block, "name")
            args = _block_attr(block, "input") or {}
            use_id = _block_attr(block, "id") or ""
            use_ids[idx] = use_id
            detector.observe(name, args)

            try:
                tool = self._registry.get(name)
            except KeyError as exc:
                results[idx] = ToolCall(
                    name=name,
                    input=args,
                    result=None,
                    error=str(exc),
                    ok=False,
                )
                continue

            coro = self._invoke_tool(tool, args, ctx)
            (write_awaitables if tool.is_write else read_awaitables).append((idx, coro))

        # Parallel reads.
        if read_awaitables:
            import asyncio

            gathered = await asyncio.gather(
                *(a for _, a in read_awaitables), return_exceptions=False
            )
            for (read_idx, _), read_call in zip(read_awaitables, gathered, strict=True):
                results[read_idx] = read_call

        # Serial writes.
        for idx, aw in write_awaitables:
            results[idx] = await aw

        # Record and shape as anthropic tool_result blocks.
        blocks: list[dict[str, Any]] = []
        for idx, call in enumerate(results):
            assert call is not None
            accum.tool_calls.append(call)
            blocks.append(
                {
                    "type": "tool_result",
                    "tool_use_id": use_ids[idx],
                    "content": _render_result(call),
                    "is_error": not call.ok,
                }
            )
        return blocks

    async def _invoke_tool(
        self,
        tool: Tool,
        raw_args: dict[str, Any],
        ctx: ToolContext,
    ) -> ToolCall:
        """Validate args, dispatch, and wrap outcome as a ToolCall."""
        log = ctx.logger.bind(tool=tool.name)
        try:
            validated = tool.input_model.model_validate(raw_args)
        except PydanticValidationError as exc:
            log.warning("tool.input_invalid", error=str(exc))
            return ToolCall(
                name=tool.name,
                input=raw_args,
                result=None,
                error=f"input validation failed: {exc}",
                ok=False,
            )

        try:
            result = await tool.handler(validated, ctx)
        except Exception as exc:
            log.warning("tool.handler_failed", error=str(exc))
            return ToolCall(
                name=tool.name,
                input=raw_args,
                result=None,
                error=f"{type(exc).__name__}: {exc}",
                ok=False,
            )

        return ToolCall(
            name=tool.name,
            input=raw_args,
            result=result,
            error=None,
            ok=True,
        )

    @staticmethod
    def _accumulate_tokens(accum: _Accumulator, response: Message) -> None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        accum.input_tokens += int(getattr(usage, "input_tokens", 0) or 0)
        accum.output_tokens += int(getattr(usage, "output_tokens", 0) or 0)


# --------------------------------------------------------------------------
# Content-block helpers.
#
# The Anthropic SDK returns SDK objects (ContentBlock subclasses). We read
# from them via duck typing so tests can pass plain dicts without pulling
# in the full SDK type machinery.
# --------------------------------------------------------------------------


def _block_type(block: Any) -> str:
    return str(_block_attr(block, "type") or "")


def _block_attr(block: Any, name: str) -> Any:
    if isinstance(block, dict):
        return block.get(name)
    return getattr(block, name, None)


def _extract_text(content: list[Any]) -> str:
    parts: list[str] = []
    for block in content:
        if _block_type(block) == "text":
            text = _block_attr(block, "text")
            if text:
                parts.append(str(text))
    return "\n".join(parts).strip()


def _dump_content(content: list[Any]) -> list[dict[str, Any]]:
    """Serialise assistant content back into a JSON-shaped list.

    Anthropic accepts either SDK blocks or dicts here; we normalise to dicts
    so the transcript is fully serialisable (helpful for audit and logs).
    """
    out: list[dict[str, Any]] = []
    for block in content:
        btype = _block_type(block)
        if btype == "text":
            out.append({"type": "text", "text": _block_attr(block, "text") or ""})
        elif btype == "tool_use":
            out.append(
                {
                    "type": "tool_use",
                    "id": _block_attr(block, "id"),
                    "name": _block_attr(block, "name"),
                    "input": _block_attr(block, "input") or {},
                }
            )
        else:
            # Unknown block type — pass through best-effort so we don't drop
            # information.
            if isinstance(block, dict):
                out.append(block)
    return out


def _render_result(call: ToolCall) -> str:
    """Format a tool outcome for the `content` of a tool_result block.

    Anthropic accepts strings or structured content; strings keep the wire
    simple and let the model read JSON naturally.
    """
    if not call.ok:
        return f"ERROR: {call.error}"
    try:
        return json.dumps(call.result, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(call.result)
