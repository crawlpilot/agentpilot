"""Pure unit tests for `agentpilot.agent.state` -- history truncation,
mocked-LLM compaction, and loop-detector nudges. No browser/network."""

from __future__ import annotations

import json

import httpx

from agentpilot.agent.state import AgentHistory, AgentStepRecord, LoopDetector
from agentpilot.llm.client import LLMConfig

CONFIG = LLMConfig(api_key="k", base_url="https://x.test", model="m", timeout_s=5.0)


def _step(n: int) -> AgentStepRecord:
    return AgentStepRecord(
        step_number=n,
        evaluation_previous_goal="ok",
        memory="",
        next_goal=f"goal {n}",
        actions=[],
        action_results=[],
    )


def test_render_summary_empty_history() -> None:
    assert AgentHistory().render_summary() == "(no steps yet)"


def test_render_summary_truncates_middle_with_omitted_marker() -> None:
    history = AgentHistory(steps=[_step(i) for i in range(20)])
    summary = history.render_summary(max_items=5)
    assert "omitted" in summary
    assert "goal 0" in summary  # first kept
    assert "goal 19" in summary  # last kept
    assert "goal 10" not in summary  # middle dropped


async def test_maybe_compact_noop_below_char_threshold() -> None:
    history = AgentHistory(steps=[_step(i) for i in range(3)])
    await history.maybe_compact(CONFIG)
    assert history.compacted_memory is None
    assert len(history.steps) == 3


async def test_maybe_compact_summarizes_and_trims_when_over_threshold(monkeypatch) -> None:
    long_goal = "x" * 2000
    history = AgentHistory(
        steps=[
            AgentStepRecord(
                step_number=i,
                evaluation_previous_goal=long_goal,
                memory="",
                next_goal=long_goal,
                actions=[],
                action_results=[],
            )
            for i in range(10)
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps({"summary": "did stuff"})}}]},
        )

    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: real_async_client(transport=httpx.MockTransport(handler), **kw),
    )

    await history.maybe_compact(CONFIG)

    assert history.compacted_memory == "did stuff"
    assert len(history.steps) == 5  # _KEEP_RECENT_STEPS
    assert "compacted_memory" in history.render_summary()


async def test_maybe_compact_failure_is_non_fatal(monkeypatch) -> None:
    long_goal = "x" * 2000
    history = AgentHistory(
        steps=[
            AgentStepRecord(
                step_number=i,
                evaluation_previous_goal=long_goal,
                memory="",
                next_goal=long_goal,
                actions=[],
                action_results=[],
            )
            for i in range(10)
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: real_async_client(transport=httpx.MockTransport(handler), **kw),
    )

    await history.maybe_compact(CONFIG)  # must not raise
    assert history.compacted_memory is None
    assert len(history.steps) == 10  # untouched on failure


def test_loop_detector_nudges_at_threshold() -> None:
    detector = LoopDetector()
    nudges = []
    for _ in range(12):
        detector.record("click", "e1")
        nudge = detector.nudge()
        if nudge:
            nudges.append(nudge)
    assert len(nudges) == 3  # thresholds (5, 8, 12)


def test_loop_detector_no_nudge_for_varied_actions() -> None:
    detector = LoopDetector()
    for i in range(12):
        detector.record("click", f"e{i}")
        assert detector.nudge() is None
