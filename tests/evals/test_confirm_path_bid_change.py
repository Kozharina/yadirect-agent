"""Eval: agent proposes a bid change and relays apply-plan instructions.

Confirm-path agent reasoning: bid changes carry no
``auto_approve_bid_change`` policy knob, so every
``set_keyword_bids`` call returns a ``status="pending"`` envelope
with a ``plan_id`` and an explicit ``next_step`` instructing the
operator to run ``yadirect-agent apply-plan <id>``. The agent must
relay ``next_step`` verbatim to the operator — operators rely on
that string being copy-pasteable, not paraphrased into "please
approve the plan in the system" or some such uselessness.

Pinned end-to-end: the model issues the bid update, gets back the
pending envelope, ends the turn with the plan id visible to the
operator. The ``FakeDirectService`` records ZERO bid writes — the
confirm tier persists the plan but does NOT reach the API.
"""

from __future__ import annotations

import pytest

from yadirect_agent.models.keywords import Keyword

from .harness import (
    EvalResult,
    FakeAnthropic,
    FakeDirectService,
    make_message,
    run_agent_eval,
    text_block,
    tool_use,
    write_policy,
)


@pytest.mark.asyncio
async def test_bid_change_returns_pending_and_relays_apply_plan(
    settings: object,
    fake_direct: FakeDirectService,
) -> None:
    write_policy(settings.agent_policy_path)  # type: ignore[attr-defined]

    # Seed a keyword with full safety-snapshot fields so KS#2 / KS#4
    # have something to work with at review time. campaign_id=1 is
    # not in ``Policy.max_cpc.campaign_max_cpc_rub`` so KS#2 is
    # unconstrained for this campaign; the new bid (5 RUB) is well
    # below any plausible cap. QS=8 is above the default
    # ``min_quality_score_for_bid_increase=5`` so KS#4 doesn't fire.
    fake_direct.keywords = [
        Keyword.model_validate(
            {
                "Id": 42,
                "AdGroupId": 100,
                "CampaignId": 1,
                "Keyword": "купить обувь",
                "State": "ON",
                "Status": "ACCEPTED",
                "Bid": 4_000_000,  # 4 RUB current
                "Productivity": {"Value": 8.0},
            }
        )
    ]

    fake_anthropic = FakeAnthropic(
        turns=[
            make_message(
                content=[
                    text_block("Raising the bid on keyword 42 to 5 RUB."),
                    tool_use(
                        "set_keyword_bids",
                        {
                            "updates": [
                                {"keyword_id": 42, "new_search_bid_rub": 5.0},
                            ]
                        },
                        id="tu_bid",
                    ),
                ],
                stop_reason="tool_use",
                input_tokens=500,
                output_tokens=80,
            ),
            # The agent has the pending envelope; relay it verbatim.
            make_message(
                content=[
                    text_block(
                        "Plan created. Operator approval required: run "
                        "`yadirect-agent apply-plan <id>` to confirm and "
                        "apply the bid change."
                    )
                ],
                stop_reason="end_turn",
                input_tokens=900,
                output_tokens=80,
            ),
        ]
    )

    run = await run_agent_eval(
        settings=settings,  # type: ignore[arg-type]
        fake_anthropic=fake_anthropic,
        user_task="Raise the bid on keyword 42 to 5 RUB.",
    )

    result = EvalResult.from_run("confirm_path_bid_change", run)
    assert result.tool_names == ("set_keyword_bids",)

    # Bid write NEVER reached DirectService — confirm tier persists
    # the plan but waits on apply-plan from the operator.
    assert fake_direct.set_keyword_bids_calls == []

    # Tool result must carry status="pending" with a plan_id and
    # the ``next_step`` operator instruction. Pin the keys explicitly
    # — operators rely on the exact response shape.
    [tool_call] = run.tool_calls
    assert tool_call.ok is True
    assert tool_call.result is not None
    assert tool_call.result["status"] == "pending"
    assert tool_call.result["plan_id"]  # non-empty
    assert "apply-plan" in tool_call.result["next_step"]

    # Two iterations: propose → end_turn. No retry loop on pending.
    assert result.iterations == 2, f"iterations regressed to {result.iterations}"

    # Final text mentions apply-plan so the operator sees the
    # next-step instruction without digging into the tool envelope.
    assert "apply-plan" in run.final_text
