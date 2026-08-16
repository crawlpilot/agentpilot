"""The agent loop: observe (snapshot) -> build prompt -> call the LLM
(structured output) -> dispatch chosen actions -> record history -> repeat
until `done`, `max_steps`, or too many consecutive failures. Mirrors
browser-use's `Agent.step()` three-phase cycle, adapted to crawlpilot's
`InteractiveSession`/`AXSnapshot`/`RefCache` primitives.
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import structlog

from agentpilot.agent.actions import (
    DEFAULT_ALLOWED_ACTIONS,
    AgentOutput,
    DoneAction,
    build_action_schema,
    parse_agent_output,
)
from agentpilot.agent.dom_view import render_snapshot_for_llm
from agentpilot.agent.judge import judge_completion
from agentpilot.agent.observation import build_observation, identity_fingerprint
from agentpilot.agent.prompts import build_system_prompt, build_user_message
from agentpilot.agent.reliability import (
    CircuitBreaker,
    CircuitBreakerTripped,
    ErrorClass,
    FailureKind,
    RetryStrategy,
    classify_error,
)
from agentpilot.agent.security import is_url_allowed, redact_secrets, substitute_secrets
from agentpilot.agent.state import AgentHistory, AgentStepRecord, LoopDetector
from agentpilot.llm.client import LLMConfig, LLMUsage, chat_json_conversation_with_usage
from agentpilot.observability.metrics import (
    agent_judge_verdicts_total,
    agent_loop_nudges_total,
    agent_runs_total,
    agent_step_llm_latency_seconds,
    agent_steps_total,
)
from agentpilot.session.interactive import InteractiveSession, execute_on_session
from agentpilot.session.registry import RegistryProtocol
from agentpilot.spi import actions as spi_actions
from agentpilot.spi.dom_tree import EnhancedDOMTreeNode
from agentpilot.spi.driver import BrowserDriver
from agentpilot.spi.errors import StaleRefError
from agentpilot.spi.snapshot import AXSnapshot, SnapshotNode

logger = structlog.get_logger(__name__)


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
    allowed_domains: tuple[str, ...] = (),
    sensitive_data: dict[str, str] | None = None,
    enable_vision: bool = False,
    enable_judge: bool = False,
    snapshot_engine: spi_actions.SnapshotEngine = "fusion",
    no_runtime: bool = False,
    max_observation_chars: int | None = 40_000,
    on_step: Callable[[AgentStepRecord], Awaitable[None]] | None = None,
) -> AgentRunResult:
    system_prompt = build_system_prompt(allowed_actions=allowed_actions, max_steps=max_steps)
    history = AgentHistory()
    loop_detector = LoopDetector()
    # Aria path keeps `AXSnapshot`; fusion path keeps the fused tree. Only one
    # is populated per run (per `snapshot_engine`), both threaded as "previous"
    # so the diff / new-element marking is relative to the prior step.
    previous_snapshot: AXSnapshot | None = None
    previous_tree: EnhancedDOMTreeNode | None = None
    use_fusion = snapshot_engine == "fusion"
    # Typed consecutive-failure counters replacing the old single counter --
    # LLM/validation/execution each get their own budget (Browser4's shape).
    # `max_failures` stays the LLM/execution budget; validation gets a little
    # more slack, mirroring Browser4's 5-vs-8 ratio.
    breaker = CircuitBreaker(
        llm_threshold=max_failures,
        validation_threshold=max_failures + 3,
        execution_threshold=max_failures,
    )
    retry = RetryStrategy()
    # Diagnostics for the exhausted/tripped exit: the most recent error message
    # and whether we stopped because the circuit breaker tripped (repeated
    # failures) vs. simply running out of steps without a `done`. Without this,
    # both cases collapse to the same opaque "max_steps_or_failures_exceeded".
    last_error: str | None = None
    circuit_broken = False

    # A viewport screenshot is added to the observation only under vision, so
    # the model can cross-reference pixels with the coordinate-tagged tree. The
    # aria path needs `with_bbox` for coordinate grounding; the fusion tree
    # already carries `absolute_position` per node, so it doesn't.
    observe_actions: list[spi_actions.Action] = [
        spi_actions.SnapshotAction(
            engine=snapshot_engine,
            no_runtime=no_runtime,
            with_bbox=not use_fusion,
            settle=True,
        ),
        spi_actions.ListTabsAction(),
    ]
    if enable_vision:
        observe_actions.append(spi_actions.ScreenshotAction())

    for step_number in range(1, max_steps + 1):
        # Observing the page is idempotent, so a transient snapshot failure is
        # retried with backoff rather than burning the whole step.
        async def _observe() -> spi_actions.ActionResult:
            return await _with_timeout(
                execute_on_session(session, observe_actions, registry=registry, driver=driver),
                step_timeout_s,
            )

        try:
            observe_result = await retry.execute(_observe)
        except Exception as exc:
            agent_steps_total.labels(outcome="observe_error").inc()
            last_error = f"failed to observe page state: {exc}"
            logger.warning("agent.observe_failed", step=step_number, error=last_error)
            history.add(_error_step(step_number, last_error))
            try:
                breaker.record_failure(FailureKind.EXECUTION)
            except CircuitBreakerTripped:
                circuit_broken = True
                break
            continue

        tabs = observe_result.tabs[0] if observe_result.tabs else []
        active_url = next((t.url for t in tabs if t.active), "")

        # The set of refs the model is actually shown this step, used to
        # pre-validate its chosen actions before dispatch (ported from
        # browser-use's `index not in selector_map` guard). Empty => unknown,
        # so validation is skipped and dispatch behaves as before.
        valid_refs: set[str] = set()
        if use_fusion:
            tree = observe_result.fused_trees[0] if observe_result.fused_trees else None
            if tree is not None:
                # Delta-first: change block + compressed tree, `*`-marking the
                # elements new since `previous_tree`.
                observation = build_observation(
                    tree, previous_tree, max_length=max_observation_chars
                )
                snapshot_text = observation.text
                # `selector_map` is keyed by int `backend_node_id`; the model
                # refers to elements as `e<backend_node_id>`.
                valid_refs = {f"e{backend_id}" for backend_id in observation.selector_map}
                page_fingerprint = identity_fingerprint(tree)
            else:
                snapshot_text, page_fingerprint = "(no snapshot)", None
            previous_tree = tree
        else:
            snapshot = observe_result.snapshots[0] if observe_result.snapshots else None
            snapshot_text = (
                render_snapshot_for_llm(snapshot, previous_snapshot)
                if snapshot
                else "(no snapshot)"
            )
            if snapshot is not None:
                valid_refs = _collect_snapshot_refs(snapshot.root)
            previous_snapshot = snapshot
            page_fingerprint = snapshot.fingerprint() if snapshot is not None else None

        # Feed a page fingerprint (active URL + stable identity) to the loop
        # detector so "page not changing despite actions" becomes a nudge.
        if page_fingerprint is not None:
            loop_detector.record_page_state(f"{active_url}\x1f{page_fingerprint}")

        await history.maybe_compact(llm_config)
        force_done = step_number == max_steps
        nudge = loop_detector.nudge()
        if nudge is not None:
            agent_loop_nudges_total.inc()
        user_message = build_user_message(
            task=task,
            history=history,
            snapshot_text=snapshot_text,
            tabs_text=_render_tabs(tabs),
            step_number=step_number,
            max_steps=max_steps,
            nudge=nudge,
        )
        schema = build_action_schema(
            allowed_actions, output_schema=output_schema, force_done=force_done
        )
        screenshot = observe_result.screenshots[0] if observe_result.screenshots else None
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": _user_content(user_message, screenshot)},
        ]

        # The LLM call is idempotent, so transient/network failures are retried
        # with backoff; a validation failure (bad output shape) is not.
        async def _call_llm(
            _messages: list[dict[str, Any]] = messages, _schema: dict[str, Any] = schema
        ) -> tuple[dict[str, Any], LLMUsage]:
            return await _with_timeout(
                chat_json_conversation_with_usage(
                    _messages, config=llm_config, json_schema=_schema
                ),
                step_timeout_s,
            )

        llm_started = time.monotonic()
        try:
            raw, usage = await retry.execute(_call_llm)
            output = parse_agent_output(raw)
        except Exception as exc:
            agent_steps_total.labels(outcome="llm_error").inc()
            last_error = f"LLM call or output parsing failed: {exc}"
            # Surface to the process log (docker logs) -- otherwise this only
            # ever appeared in the run's in-memory history / final API error,
            # making a broken LLM endpoint or missing model invisible to a
            # `docker compose logs -f worker`.
            logger.warning(
                "agent.llm_failed",
                step=step_number,
                error=last_error,
                model=llm_config.model,
                base_url=llm_config.base_url,
            )
            history.add(_error_step(step_number, last_error))
            kind = (
                FailureKind.VALIDATION
                if classify_error(exc) is ErrorClass.VALIDATION
                else FailureKind.LLM
            )
            try:
                breaker.record_failure(kind)
            except CircuitBreakerTripped:
                circuit_broken = True
                break
            continue
        step_duration_ms = int((time.monotonic() - llm_started) * 1000)
        agent_step_llm_latency_seconds.observe(step_duration_ms / 1000)
        _log_step_response(step_number, output)

        done = next((a for a in output.actions if isinstance(a, DoneAction)), None)
        driver_actions = [a for a in output.actions if not isinstance(a, DoneAction)]

        action_results: list[str] = []
        step_outcome = "ok"
        tripped = False
        if driver_actions:
            for action in driver_actions:
                loop_detector.record(type(action).__name__, _action_key(action))

            # Ref pre-validation (ported from browser-use `tools/service.py`'s
            # `index not in selector_map` guard): a ref the current page state
            # never issued -- a hallucinated ref, a stale one, or a URL passed
            # as `ref` -- becomes actionable feedback the model reads next step,
            # not an action dispatched blindly to fail opaquely inside the
            # driver. Skipped entirely when `valid_refs` is unknown (empty).
            valid_actions: list[spi_actions.Action] = []
            for action in driver_actions:
                ref = _action_ref(action)
                if ref is not None and valid_refs and ref not in valid_refs:
                    step_outcome = "invalid_ref"
                    action_results.append(
                        f"Element with ref {ref!r} does not exist on the current page. "
                        "Only use a ref shown in the current page state (e.g. 'e12'). "
                        "To open a URL, use the navigate action, not click."
                    )
                else:
                    valid_actions.append(action)

            blocked = [
                a
                for a in valid_actions
                if isinstance(a, spi_actions.NavigateAction)
                and not is_url_allowed(a.url, allowed_domains)
            ]
            if blocked:
                # Refuse the whole step rather than dispatch a partial batch --
                # the model re-plans next step seeing the refusal.
                step_outcome = "blocked"
                action_results.extend(
                    f"navigation to {a.url} was blocked: outside the allowed domains for this run"
                    for a in blocked
                )
            elif valid_actions:
                # Substitute secrets into the *dispatched copy* only; the
                # recorded `driver_actions` keep their placeholder form, so
                # secrets never enter the persisted step. Action dispatch is
                # not retried -- a half-applied batch is not safely repeatable.
                dispatch_actions = [_apply_secrets(a, sensitive_data) for a in valid_actions]
                try:
                    dispatch_result = await _with_timeout(
                        execute_on_session(
                            session, dispatch_actions, registry=registry, driver=driver
                        ),
                        step_timeout_s,
                    )
                    verifications = [
                        redact_secrets(v, sensitive_data) for v in dispatch_result.verifications
                    ]
                    action_results.extend(verifications)
                    if dispatch_result.sequence_aborted:
                        step_outcome = "sequence_aborted"
                        action_results.append(
                            "one or more actions in this step were skipped: an earlier action "
                            "unexpectedly changed the page -- re-observe before continuing"
                        )
                    elif not verifications:
                        action_results.append("actions dispatched successfully")
                    breaker.reset()  # progress -> clear consecutive-failure history
                except StaleRefError as exc:
                    # A ref that passed pre-validation but no longer resolves at
                    # dispatch time: the DOM mutated between snapshot and action.
                    # Reword to actionable re-observe guidance (browser-use:
                    # "page may have changed. Try refreshing browser state.")
                    # instead of surfacing the raw driver error.
                    step_outcome = "action_failed"
                    last_error = (
                        f"could not act on ref {exc.ref!r}: the page changed since it was "
                        "captured. Re-observe the current page state before retrying."
                    )
                    action_results.append(last_error)
                    try:
                        breaker.record_failure(FailureKind.EXECUTION)
                    except CircuitBreakerTripped:
                        tripped = True
                except Exception as exc:
                    step_outcome = "action_failed"
                    last_error = redact_secrets(f"action failed: {exc}", sensitive_data)
                    action_results.append(last_error)
                    try:
                        breaker.record_failure(FailureKind.EXECUTION)
                    except CircuitBreakerTripped:
                        tripped = True
            # else: every action had an invalid ref -- nothing dispatched. That
            # is model-correctable feedback, not an execution failure, so the
            # breaker is left untouched (mirrors browser-use returning an
            # ActionResult error rather than raising); the model re-plans from
            # the `invalid_ref` results next step.
        agent_steps_total.labels(outcome=step_outcome).inc()

        step_record = AgentStepRecord(
            step_number=step_number,
            evaluation_previous_goal=output.evaluation_previous_goal,
            memory=output.memory,
            next_goal=output.next_goal,
            actions=[_action_to_dict(a) for a in driver_actions],
            action_results=action_results,
            thinking=output.thinking,
            duration_ms=step_duration_ms,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            screenshot=screenshot,
        )
        history.add(step_record)
        _log_step_completion(step_record, step_outcome)
        if on_step is not None:
            await on_step(step_record)

        if done is not None:
            success = done.success
            result_text = done.result
            # An independent, skeptical judge can veto a self-reported success
            # (never a self-reported failure). Fail-open on judge error.
            if enable_judge and done.success:
                verdict = await judge_completion(
                    task=task,
                    claimed_result=done.result,
                    extracted_data=done.extracted_data,
                    page_state=snapshot_text,
                    history_summary=history.render_summary(),
                    config=llm_config,
                )
                label = "error" if verdict.errored else "passed" if verdict.passed else "rejected"
                agent_judge_verdicts_total.labels(verdict=label).inc()
                if not verdict.passed:
                    success = False
                    result_text = (
                        f"{done.result}\n\n[independent judge rejected this completion: "
                        f"{verdict.reason}]"
                    )
            agent_runs_total.labels(outcome="success" if success else "failed").inc()
            return AgentRunResult(
                success=success,
                result=result_text,
                extracted_data=done.extracted_data,
                steps=history,
            )
        if tripped:
            circuit_broken = True
            break

    agent_runs_total.labels(outcome="exhausted").inc()
    if circuit_broken:
        # Stopped early on repeated failures -- the last error is almost always
        # the actionable one (bad model output, an unreachable page, etc.).
        result = "agent stopped after repeated failures"
        if last_error:
            result += f": {last_error}"
        error = "circuit_breaker_tripped"
        logger.error("agent.run_aborted", reason=result)
    else:
        result = f"agent ran out of steps ({max_steps}) without calling done"
        if last_error:
            result += f" -- last error: {last_error}"
        error = "max_steps_exceeded"
    return AgentRunResult(
        success=False,
        result=result,
        extracted_data=None,
        steps=history,
        error=error,
    )


def _eval_emoji(evaluation: str) -> str:
    """browser-use's `_log_response` heuristic: colour the eval by whether the
    model judged its own last action a success/failure/neither."""

    lowered = evaluation.lower()
    if "success" in lowered:
        return "👍"
    if "failure" in lowered:
        return "⚠️"
    return "❔"


def _log_step_response(step_number: int, output: AgentOutput) -> None:
    """Per-step flow logging ported from browser-use's `_log_response`: the
    model's evaluation / memory / next goal, so `docker compose logs -f worker`
    shows what the agent is doing -- not just failures."""

    if output.thinking:
        logger.debug("💡 Thinking", step=step_number, thinking=output.thinking)
    if output.evaluation_previous_goal:
        logger.info(
            f"{_eval_emoji(output.evaluation_previous_goal)} Eval: {output.evaluation_previous_goal}",
            step=step_number,
        )
    if output.memory:
        logger.info(f"🧠 Memory: {output.memory}", step=step_number)
    if output.next_goal:
        logger.info(f"🎯 Next goal: {output.next_goal}", step=step_number)


def _log_step_completion(step: AgentStepRecord, outcome: str) -> None:
    """browser-use's `_log_step_completion_summary`: action count, timing, and
    outcome for the step just executed."""

    logger.info(
        f"📍 Step {step.step_number}: {len(step.actions)} "
        f"action{'' if len(step.actions) == 1 else 's'} -> {outcome}",
        step=step.step_number,
        actions=len(step.actions),
        outcome=outcome,
        duration_ms=step.duration_ms,
        input_tokens=step.input_tokens,
        output_tokens=step.output_tokens,
    )


def _collect_snapshot_refs(root: SnapshotNode) -> set[str]:
    """Every ref the aria-path snapshot issued this step (fusion has its own
    `selector_map`). Used to pre-validate the model's chosen refs."""

    refs: set[str] = set()
    stack = [root]
    while stack:
        node = stack.pop()
        if node.ref:
            refs.add(node.ref)
        stack.extend(node.children)
    return refs


def _action_ref(action: spi_actions.Action) -> str | None:
    """The element ref an action targets, or `None` for ref-less actions
    (navigate, press, wait, scroll-without-ref, ...)."""

    ref = getattr(action, "ref", None)
    return ref if isinstance(ref, str) and ref else None


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


def _user_content(text: str, screenshot: bytes | None) -> str | list[dict[str, Any]]:
    """Plain text when vision is off; otherwise the OpenAI multimodal parts
    list with the viewport screenshot attached as a base64 data URL."""

    if screenshot is None:
        return text
    b64 = base64.b64encode(screenshot).decode("ascii")
    hint = (
        f"{text}\n\nA screenshot of the current viewport is attached; use it "
        "together with the accessibility tree above to locate elements."
    )
    return [
        {"type": "text", "text": hint},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
    ]


def _apply_secrets(
    action: spi_actions.Action, sensitive_data: dict[str, str] | None
) -> spi_actions.Action:
    """Return a dispatch-ready copy with secrets substituted into fill text;
    every other action is passed through unchanged."""

    if sensitive_data and isinstance(action, spi_actions.FillAction):
        return dataclasses.replace(action, text=substitute_secrets(action.text, sensitive_data))
    return action


def _action_key(action: spi_actions.Action) -> str:
    return str(getattr(action, "ref", None) or getattr(action, "url", None) or "")


def _action_to_dict(action: spi_actions.Action) -> dict[str, Any]:
    return {"type": type(action).__name__, **vars(action)}
