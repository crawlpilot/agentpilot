"""Per-step prompt assembly for the agent loop: a markdown system-prompt
template (`prompt_templates/system_prompt.md`, loaded once, `.format()`'d
with the rendered action list and `max_steps`) plus a per-step user-message
builder, mirroring browser-use's `SystemPrompt`/`AgentMessagePrompt` split.
"""

from __future__ import annotations

from pathlib import Path

from agentpilot.agent.actions import DEFAULT_ALLOWED_ACTIONS, render_action_descriptions
from agentpilot.agent.state import AgentHistory

_SYSTEM_PROMPT_PATH = Path(__file__).parent / "prompt_templates" / "system_prompt.md"


def build_system_prompt(
    *, allowed_actions: tuple[str, ...] = DEFAULT_ALLOWED_ACTIONS, max_steps: int
) -> str:
    template = _SYSTEM_PROMPT_PATH.read_text()
    return template.format(actions=render_action_descriptions(allowed_actions), max_steps=max_steps)


def build_user_message(
    *,
    task: str,
    history: AgentHistory,
    snapshot_text: str,
    tabs_text: str,
    step_number: int,
    max_steps: int,
    nudge: str | None = None,
) -> str:
    parts = [
        f"<user_request>\n{task}\n</user_request>",
        f"<agent_history>\n{history.render_summary()}\n</agent_history>",
        f"<agent_state>\nStep {step_number} of {max_steps}.\n</agent_state>",
        f"<browser_state>\n{snapshot_text}\n\nOpen tabs:\n{tabs_text}\n</browser_state>",
    ]
    if nudge:
        parts.append(f"<nudge>\n{nudge}\n</nudge>")
    return "\n\n".join(parts)
