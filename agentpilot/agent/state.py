"""Per-run agent state: step history (with bounded, LLM-summarizable
truncation) and loop/repetition detection. Mirrors browser-use's
`MessageManager` history-management discipline -- a compact, roughly
constant-size text summary fed into each step's prompt, not a raw
append-only transcript -- without needing a stateful message-list object of
its own (the agent loop rebuilds messages fresh every step, see `loop.py`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from agentpilot.llm.client import LLMConfig, chat_json

_COMPACT_TRIGGER_CHARS = 8_000
"""Once the rendered history text exceeds this, `maybe_compact` summarizes
it via one LLM call rather than growing the prompt unboundedly."""

_KEEP_RECENT_STEPS = 5
"""After compaction, only this many most-recent steps are kept verbatim
alongside the new summary."""

_COMPACT_SYSTEM_PROMPT = (
    "Summarize the following web-agent step history into a few sentences, "
    "preserving concrete facts (URLs visited, data found, decisions made). "
    "This summary will stand in for the detailed history in future prompts."
)


@dataclass
class AgentStepRecord:
    step_number: int
    evaluation_previous_goal: str
    memory: str
    next_goal: str
    actions: list[dict[str, Any]]
    action_results: list[str]
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class AgentHistory:
    steps: list[AgentStepRecord] = field(default_factory=list)
    compacted_memory: str | None = None

    def add(self, step: AgentStepRecord) -> None:
        self.steps.append(step)

    def render_summary(self, max_items: int = 10) -> str:
        lines: list[str] = []
        if self.compacted_memory:
            lines.append(
                "<compacted_memory> (unverified summary of earlier steps -- "
                "do not report something as done solely because it's "
                f"mentioned here): {self.compacted_memory}"
            )

        steps = self.steps
        if len(steps) > max_items:
            omitted = len(steps) - max_items
            shown = [steps[0], *steps[-(max_items - 1) :]]
            lines.append(_render_step(shown[0]))
            lines.append(f"... {omitted} step(s) omitted ...")
            lines.extend(_render_step(s) for s in shown[1:])
        else:
            lines.extend(_render_step(s) for s in steps)

        return "\n".join(lines) if lines else "(no steps yet)"

    async def maybe_compact(self, config: LLMConfig) -> None:
        rendered = self.render_summary(max_items=len(self.steps) or 1)
        if len(rendered) <= _COMPACT_TRIGGER_CHARS or len(self.steps) <= _KEEP_RECENT_STEPS:
            return
        try:
            result = await chat_json(
                _COMPACT_SYSTEM_PROMPT,
                rendered,
                config=config,
                json_schema={
                    "type": "object",
                    "properties": {"summary": {"type": "string"}},
                    "required": ["summary"],
                },
            )
        except Exception:
            return  # best-effort: keep the untruncated history rather than fail the run
        self.compacted_memory = result.get("summary", self.compacted_memory)
        self.steps = self.steps[-_KEEP_RECENT_STEPS:]


def _render_step(step: AgentStepRecord) -> str:
    results = "; ".join(step.action_results) if step.action_results else "(no result)"
    return (
        f"Step {step.step_number}: goal={step.next_goal!r} "
        f"evaluation={step.evaluation_previous_goal!r} result={results}"
    )


class LoopDetector:
    """Hashes recent `(action_type, key_params)` tuples; returns an
    escalating nudge string once the same action repeats too often --
    mirrors browser-use's `_inject_loop_detection_nudge`."""

    _THRESHOLDS = (5, 8, 12)

    def __init__(self) -> None:
        self._recent: list[tuple[str, str]] = []

    def record(self, action_type: str, key: str) -> None:
        self._recent.append((action_type, key))

    def nudge(self) -> str | None:
        if not self._recent:
            return None
        last = self._recent[-1]
        count = sum(1 for entry in self._recent if entry == last)
        if count in self._THRESHOLDS:
            return (
                f"Heads up: you have repeated the action {last[0]!r} with "
                f"similar parameters {count} times. If this isn't making "
                "progress, consider a different approach."
            )
        return None
