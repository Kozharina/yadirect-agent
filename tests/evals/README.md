# Agent evals

Typed-task regression tests for agent reasoning. Each eval is a
``pytest`` test that wires the real ``Agent`` loop and the real tool
registry against a scripted ``FakeAnthropic`` and an in-memory
``DirectService`` fake — no real Claude API calls, no real Direct
API calls.

The point isn't to test individual modules (we have unit tests for
that). The point is to catch regressions in **agent reasoning**:
does the model still pick the right tool given a user task? Does it
recover from a ``PlanRejected``? Does it relay ``next_step`` to the
operator on the confirm path?

## Running

```bash
make evals
# or:
pytest tests/evals/ -v
```

CI runs evals only when ``RUN_EVALS=1`` is set (cost-controlled
toggle; the framework itself is cost-free, but full M7.2
later-stage evals may go through the real Anthropic API).

## Adding an eval

One ``test_*.py`` file per scenario. Each test:

1. Builds a ``FakeAnthropic`` with scripted turns describing what
   the model would do step by step. Use ``script_*`` helpers from
   ``harness.py`` for common turn shapes.
2. Seeds the ``fake_direct`` fixture with the campaign / keyword
   state the scenario expects.
3. Calls ``run_agent_eval(...)``, gets back the ``AgentRun``.
4. Asserts on tool calls, final text, and the metrics
   (``iterations``, ``input_tokens``, ``output_tokens``).

The metrics are recorded for every eval; pytest's terminal output
shows them so a reviewer can see "this eval used 3 iterations and
1200 tokens" before deciding whether the agent's reasoning got
more or less efficient.

## What's covered today

- ``test_pause_low_ctr.py`` — happy path: agent lists campaigns,
  pauses the right ones based on a CTR criterion in the user
  task.
- ``test_safety_reject_budget_cap.py`` — reject path: agent tries
  to set a budget over the policy cap, gets ``PlanRejected``,
  reports cleanly.
- ``test_confirm_path_bid_change.py`` — confirm path: agent
  proposes a bid change, gets ``PlanRequired``, relays the
  ``next_step`` (apply-plan command) to the operator.

## Out of scope (for now)

- Real Anthropic API calls. The framework supports them in
  principle — pass a real ``AsyncAnthropic`` client — but the cost
  story isn't built yet.
- Token cost computation. ``input_tokens`` / ``output_tokens``
  are surfaced; rubles-per-token conversion belongs to the
  reporting layer.
- Replay-mode evals (record a real session, replay against
  fakes for prompt-change regression). Tracked separately in
  BACKLOG ideas.
