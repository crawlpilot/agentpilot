"""The agent loop: observe (snapshot) -> build prompt -> call the LLM
(structured output) -> dispatch chosen actions -> record history -> repeat
until `done`, `max_steps`, or too many consecutive failures. Mirrors
browser-use's `Agent.step()` three-phase cycle, adapted to crawlpilot's
`InteractiveSession`/`AXSnapshot`/`RefCache` primitives.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from agentpilot.agent.actions import (
    DEFAULT_ALLOWED_ACTIONS,
    DoneAction,
    build_action_schema,
    parse_agent_output,
)
from agentpilot.agent.dom_view import render_snapshot_for_llm
from agentpilot.agent.prompts import build_system_prompt, build_user_message
from agentpilot.agent.state import AgentHistory, AgentStepRecord, LoopDetector
from agentpilot.llm.client import LLMConfig, chat_json_conversation
from agentpilot.session.interactive import InteractiveSession, execute_on_session
from agentpilot.session.registry import RegistryProtocol
from agentpilot.spi import actions as spi_actions
from agentpilot.spi.driver import BrowserDriver
from agentpilot.spi.snapshot import AXSnapshot


async def _with_timeout[T](coro: Awaitable[T], timeout: float | None) -> T:
    """Bounds one network-ish await (a browser action, an LLM call) so a
    single hung step can't block a run forever -- `timeout=None` (the
    default) disables this and awaits normally."""

    if timeout is None:
        return await coro
    return await asyncio.wait_for(coro, timeout=timeout)


@dataclass
class AgentRunResult:
    success: bool
    result: str
    extracted_data: dict[str, Any] | None
    steps: AgentHistory
    error: str | None = None


async def run_agent_loop(
    *,
    task: str,
    session: InteractiveSession,
    registry: RegistryProtocol,
    driver: BrowserDriver,
    llm_config: LLMConfig,
    max_steps: int,
    allowed_actions: tuple[str, ...] = DEFAULT_ALLOWED_ACTIONS,
    output_schema: dict[str, Any] | None = None,
    max_failures: int = 5,
    step_timeout_s: float | None = None,
    on_step: Callable[[AgentStepRecord], Awaitable[None]] | None = None,
) -> AgentRunResult:
    system_prompt = build_system_prompt(allowed_actions=allowed_actions, max_steps=max_steps)
    history = AgentHistory()
    loop_detector = LoopDetector()
    previous_snapshot: AXSnapshot | None = None
    consecutive_failures = 0

    for step_number in range(1, max_steps + 1):
        try:
            observe_result = await _with_timeout(
                execute_on_session(
                    session,
                    [spi_actions.SnapshotAction(with_bbox=True), spi_actions.ListTabsAction()],
                    registry=registry,
                    driver=driver,
                ),
                step_timeout_s,
            )
        except Exception as exc:
            consecutive_failures += 1
            history.add(_error_step(step_number, f"failed to observe page state: {exc}"))
            if consecutive_failures >= max_failures:
                break
            continue

        snapshot = observe_result.snapshots[0] if observe_result.snapshots else None
        snapshot_text = (
            render_snapshot_for_llm(snapshot, previous_snapshot) if snapshot else "(no snapshot)"
        )
        tabs = observe_result.tabs[0] if observe_result.tabs else []
        previous_snapshot = snapshot

        await history.maybe_compact(llm_config)
        force_done = step_number == max_steps
        user_message = build_user_message(
            task=task,
            history=history,
            snapshot_text=snapshot_text,
            tabs_text=_render_tabs(tabs),
            step_number=step_number,
            max_steps=max_steps,
            nudge=loop_detector.nudge(),
        )
        schema = build_action_schema(
            allowed_actions, output_schema=output_schema, force_done=force_done
        )
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]

        try:
            raw = await _with_timeout(
                chat_json_conversation(messages, config=llm_config, json_schema=schema),
                step_timeout_s,
            )
            output = parse_agent_output(raw)
        except Exception as exc:
            consecutive_failures += 1
            history.add(_error_step(step_number, f"LLM call or output parsing failed: {exc}"))
            if consecutive_failures >= max_failures:
                break
            continue

        done = next((a for a in output.actions if isinstance(a, DoneAction)), None)
        driver_actions = [a for a in output.actions if not isinstance(a, DoneAction)]

        action_results: list[str] = []
        if driver_actions:
            for action in driver_actions:
                loop_detector.record(type(action).__name__, _action_key(action))
            try:
                dispatch_result = await _with_timeout(
                    execute_on_session(session, driver_actions, registry=registry, driver=driver),
                    step_timeout_s,
                )
                if dispatch_result.sequence_aborted:
                    action_results.append(
                        "one or more actions in this step were skipped: an earlier action "
                        "unexpectedly changed the page -- re-observe before continuing"
                    )
                else:
                    action_results.append("actions dispatched successfully")
                consecutive_failures = 0
            except Exception as exc:
                action_results.append(f"action failed: {exc}")
                consecutive_failures += 1

        step_record = AgentStepRecord(
            step_number=step_number,
            evaluation_previous_goal=output.evaluation_previous_goal,
            memory=output.memory,
            next_goal=output.next_goal,
            actions=[_action_to_dict(a) for a in driver_actions],
            action_results=action_results,
        )
        history.add(step_record)
        if on_step is not None:
            await on_step(step_record)

        if done is not None:
            return AgentRunResult(
                success=done.success,
                result=done.result,
                extracted_data=done.extracted_data,
                steps=history,
            )
        if consecutive_failures >= max_failures:
            break

    return AgentRunResult(
        success=False,
        result="agent did not call done within the step/failure budget",
        extracted_data=None,
        steps=history,
        error="max_steps_or_failures_exceeded",
    )


def _error_step(step_number: int, message: str) -> AgentStepRecord:
    return AgentStepRecord(
        step_number=step_number,
        evaluation_previous_goal="",
        memory="",
        next_goal="",
        actions=[],
        action_results=[message],
    )


def _render_tabs(tabs: list[spi_actions.TabInfo]) -> str:
    if not tabs:
        return "(none)"
    return "\n".join(
        f"- {t.page_id}: {t.url} ({'active' if t.active else 'background'})" for t in tabs
    )


def _action_key(action: spi_actions.Action) -> str:
    return str(getattr(action, "ref", None) or getattr(action, "url", None) or "")


def _action_to_dict(action: spi_actions.Action) -> dict[str, Any]:
    return {"type": type(action).__name__, **vars(action)}
