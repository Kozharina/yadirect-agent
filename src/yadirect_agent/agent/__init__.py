"""Agent layer: tool registry, agent loop, prompts, safety.

This is where decisions are made. Tools are thin wrappers over services;
the loop owns multi-step reasoning via Anthropic tool use. Safety policy
and audit live alongside but are sourced from later milestones (see
docs/TECHNICAL_SPEC.md §M2).
"""

from .loop import Agent, AgentRun, MaxIterationsExceededError, RepetitionDetectedError, ToolCall
from .prompts import SYSTEM_PROMPT
from .tools import Tool, ToolContext, ToolRegistry, build_default_registry

__all__ = [
    "SYSTEM_PROMPT",
    "Agent",
    "AgentRun",
    "MaxIterationsExceededError",
    "RepetitionDetectedError",
    "Tool",
    "ToolCall",
    "ToolContext",
    "ToolRegistry",
    "build_default_registry",
]
