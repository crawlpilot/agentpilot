"""Pure unit tests for `agentpilot.agent.prompts` -- template rendering, no
browser/network."""

from __future__ import annotations

from agentpilot.agent.prompts import build_system_prompt, build_user_message
from agentpilot.agent.state import AgentHistory, AgentStepRecord


def test_build_system_prompt_substitutes_actions_and_max_steps() -> None:
    text = build_system_prompt(max_steps=25)
    assert "{actions}" not in text
    assert "{max_steps}" not in text
    assert "25 steps" in text
    assert "- click:" in text
    assert "- done:" in text


def test_build_user_message_includes_task_and_snapshot() -> None:
    history = AgentHistory()
    text = build_user_message(
        task="find the price",
        history=history,
        snapshot_text="[e1]<button \"Buy\"/>",
        tabs_text="tab-1: https://example.com",
        step_number=1,
        max_steps=10,
    )
    assert "find the price" in text
    assert "[e1]<button" in text
    assert "Step 1 of 10" in text
    assert "(no steps yet)" in text


def test_build_user_message_includes_nudge_when_given() -> None:
    text = build_user_message(
        task="t",
        history=AgentHistory(),
        snapshot_text="",
        tabs_text="",
        step_number=1,
        max_steps=1,
        nudge="stop repeating yourself",
    )
    assert "<nudge>" in text
    assert "stop repeating yourself" in text


def test_build_user_message_renders_history_steps() -> None:
    history = AgentHistory()
    history.add(
        AgentStepRecord(
            step_number=1,
            evaluation_previous_goal="n/a",
            memory="",
            next_goal="click buy",
            actions=[{"type": "click", "ref": "e1"}],
            action_results=["clicked"],
        )
    )
    text = build_user_message(
        task="t", history=history, snapshot_text="", tabs_text="", step_number=2, max_steps=5
    )
    assert "click buy" in text
