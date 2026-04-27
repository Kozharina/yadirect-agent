"""Agent layer: tool registry, agent loop, prompts, safety.

This is where decisions are made. Tools are thin wrappers over services;
the loop owns multi-step reasoning via Anthropic tool use. Safety policy
and audit live alongside but are sourced from later milestones (see
docs/TECHNICAL_SPEC.md §M2).

This ``__init__`` deliberately re-exports nothing. The public surface
of the package is the submodule hierarchy (``yadirect_agent.agent.loop``,
``yadirect_agent.agent.tools``, ``yadirect_agent.agent.executor``,
etc.) — eager re-exports here would form an import cycle with
``yadirect_agent.services.campaigns``, which now imports
``yadirect_agent.agent.executor`` for the ``@requires_plan`` decorator.
Callers who want a flat namespace can build their own.
"""
