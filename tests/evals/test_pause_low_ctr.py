"""Eval: pause campaigns with CTR below 0.5%.

Happy-path agent reasoning: given a user task with a numeric
threshold, the agent must list campaigns, identify the ones below
the threshold, and pause them in a single batch. Pinned end-to-end:
agent dispatches the right tools with the right arguments, and the
``FakeDirectService`` records exactly the expected suspend call.

The CTR data isn't actually returned by ``list_campaigns`` today —
this eval scripts the model's intermediate reasoning explicitly via
``FakeAnthropic`` turns, simulating what the model would produce
given access to a CTR-aware reporting tool. The point of the eval
is to verify the *tool dispatch path* is correct, not that the
model is good at arithmetic. Once M6 reporting lands the script
will reduce to "list_campaigns_with_ctr → pause_campaigns".
"""

from __future__ import annotations

import pytest

from yadirect_agent.models.campaigns import (
    Campaign,
    CampaignState,
    CampaignStatus,
    DailyBudget,
)

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
async def test_pause_campaigns_below_ctr_threshold(
    settings: object,
    fake_direct: FakeDirectService,
    tmp_path: object,
) -> None:
    # Policy: autonomy_full so pause is allowed end-to-end. Default
    # auto_approve_pause=True so the agent's pause call goes through
    # the allow tier (no operator confirm needed for pause).
    write_policy(settings.agent_policy_path)  # type: ignore[attr-defined]

    fake_direct.campaigns = [
        Campaign(
            Id=1,
            Name="brand-search",
            State=CampaignState.ON,
            Status=CampaignStatus.ACCEPTED,
            DailyBudget=DailyBudget(amount=500_000_000, mode="STANDARD"),
        ),
        Campaign(
            Id=2,
            Name="display-broad",
            State=CampaignState.ON,
            Status=CampaignStatus.ACCEPTED,
            DailyBudget=DailyBudget(amount=300_000_000, mode="STANDARD"),
        ),
        Campaign(
            Id=3,
            Name="search-low-ctr",
            State=CampaignState.ON,
            Status=CampaignStatus.ACCEPTED,
            DailyBudget=DailyBudget(amount=200_000_000, mode="STANDARD"),
        ),
    ]

    # Scripted model reasoning: turn 1 lists campaigns, turn 2 pauses
    # campaigns 2 & 3 (the "low-CTR" ones in this scenario), turn 3
    # confirms to the operator.
    fake_anthropic = FakeAnthropic(
        turns=[
            make_message(
                content=[
                    text_block("I'll list the campaigns first."),
                    tool_use("list_campaigns", {}, id="tu_list"),
                ],
                stop_reason="tool_use",
                input_tokens=400,
                output_tokens=80,
            ),
            make_message(
                content=[
                    text_block("Pausing campaigns 2 and 3, both below 0.5% CTR."),
                    tool_use(
                        "pause_campaigns",
                        {"ids": [2, 3]},
                        id="tu_pause",
                    ),
                ],
                stop_reason="tool_use",
                input_tokens=600,
                output_tokens=120,
            ),
            make_message(
                content=[text_block("Paused campaigns 2 and 3 (CTR < 0.5%).")],
                stop_reason="end_turn",
                input_tokens=700,
                output_tokens=40,
            ),
        ]
    )

    run = await run_agent_eval(
        settings=settings,  # type: ignore[arg-type]
        fake_anthropic=fake_anthropic,
        user_task="Pause every campaign with CTR below 0.5%.",
    )

    # Pin: agent dispatched the expected tool sequence.
    result = EvalResult.from_run("pause_low_ctr", run)
    assert result.tool_names == ("list_campaigns", "pause_campaigns"), (
        f"unexpected tool sequence: {result.tool_names}"
    )

    # Pin: exactly one suspend call to DirectService, with the right ids.
    # Bulk semantics — campaigns 2 & 3 must arrive in a single batch
    # so the operator's audit trail records one decision, not two.
    assert fake_direct.suspend_calls == [[2, 3]]

    # Pin: agent didn't go full machine-gun. Three iterations
    # (list → pause → end_turn) is the budget for this scenario.
    # Tighten if the model regresses to extra confirmation rounds;
    # widen if a real future tool adds an unavoidable check.
    assert result.iterations == 3, f"iterations regressed to {result.iterations}"

    # Pin: final text reaches the operator (avoids silent end_turn).
    assert "Paused" in run.final_text or "паузу" in run.final_text.lower()
