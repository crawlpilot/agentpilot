"""LLM-facing action schema and parser.

Deliberately a separate Pydantic mirror of `spi.actions.Action`, not a reuse
of `gateway.schemas.ActionIn`/`gateway.action_conversion.to_spi_action`:
`agentpilot.agent` sits *below* `agentpilot.gateway` in the layering
(`gateway -> agent -> session -> llm -> spi`) and must not import it. Both
mirrors describe the same underlying dataclasses for two different
consumers -- HTTP-boundary validation vs. this module's LLM tool schema.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from crawlpilot.spi import actions as spi_actions
from crawlpilot.tools import browser_tools

DEFAULT_ALLOWED_ACTIONS: tuple[str, ...] = (
    # Navigation and the core interaction verbs first -- most-used, and the ones
    # a model should reach for by default.
    "navigate",
    "go_back",
    "click",
    "fill",
    "select_option",
    "dropdown_options",
    "hover",
    "press",
    "send_keys",
    "scroll",
    "find_text",
    "wait",
    # Dialogs: a `confirm()` blocks the page until it is answered, so a model
    # that cannot answer one is simply stuck. `dialog_status` first -- it is the
    # read-only probe for "did that click ask me something?".
    "dialog_status",
    "dialog_accept",
    "dialog_dismiss",
    # Reading the page: the cheap targeted queries before the expensive full read.
    "search_page",
    "find_elements",
    "extract",
    "screenshot",
    # Tabs last -- needed, but rarely the right first move.
    "new_tab",
    "switch_tab",
    "close_tab",
)
"""Order is a presentation choice -- most-used first, since it is the order a
model reads the schema in -- so it stays written out rather than derived from
catalog order. *Membership* is not: `test_tools_registry` asserts this set equals
the registry's `agent_exposed` subset, so a verb cannot be exposed to agents in
one place and forgotten in the other.

`execute_js`, `upload_file`, `snapshot` and `list_tabs` are absent because their
catalog entries declare `agent_fields=None` -- arbitrary JS and a driver-side
file path are security-sensitive, and the other two would only return what the
model was just shown. All remain reachable by passing a wider `allowed_actions`,
and the exclusion lives *with* the verb rather than in a separate tuple that had
to be kept in step with it.
"""

_AGENT_TOOLS = browser_tools().subset(agent_exposed=True)

_ACTION_MODELS: dict[str, type[BaseModel]] = {
    spec.name: spec.agent_model() for spec in _AGENT_TOOLS
}
"""Generated from `tools.CATALOG`. These were 11 hand-written Pydantic models,
a deliberate second copy of the same vocabulary the HTTP boundary already
mirrored -- this module's own docstring used to concede as much (plan D5)."""

_ACTION_DESCRIPTIONS: dict[str, str] = {
    spec.name: spec.description for spec in _AGENT_TOOLS
}

_SPECS_BY_NAME = {spec.name: spec for spec in _AGENT_TOOLS}


@dataclass
class DoneAction:
    """The agent-loop-only terminal action -- never dispatched to the
    driver, not part of `spi.actions.Action`. Ends `run_agent_loop`."""

    success: bool
    result: str
    extracted_data: dict[str, Any] | None = None


@dataclass
class AgentOutput:
    evaluation_previous_goal: str
    memory: str
    next_goal: str
    actions: list[spi_actions.Action | DoneAction]
    thinking: str | None = None


def render_action_descriptions(
    allowed_actions: tuple[str, ...] = DEFAULT_ALLOWED_ACTIONS,
) -> str:
    lines = [f"- {name}: {_ACTION_DESCRIPTIONS[name]}" for name in allowed_actions]
    lines.append(
        "- done: call this when the task is complete (or cannot be completed) to end the run."
    )
    return "\n".join(lines)


def build_action_schema(
    allowed_actions: tuple[str, ...] = DEFAULT_ALLOWED_ACTIONS,
    *,
    output_schema: dict[str, Any] | None = None,
    force_done: bool = False,
) -> dict[str, Any]:
    """Assembles the "AgentOutput" JSON schema handed to `chat_json_conversation`'s
    `json_schema` param. `force_done=True` (the loop's final allowed step)
    narrows `action` so `done` is the only valid choice -- mirrors
    browser-use's `_force_done_after_last_step`."""

    action_names = ("done",) if force_done else (*allowed_actions, "done")
    action_schemas = [_action_json_schema(name, output_schema) for name in action_names]

    return {
        "type": "object",
        "properties": {
            "thinking": {"type": "string"},
            "evaluation_previous_goal": {"type": "string"},
            "memory": {"type": "string"},
            "next_goal": {"type": "string"},
            "action": {
                "type": "array",
                "items": {"anyOf": action_schemas},
                "minItems": 1,
            },
        },
        "required": ["evaluation_previous_goal", "memory", "next_goal", "action"],
    }


def _action_json_schema(name: str, output_schema: dict[str, Any] | None) -> dict[str, Any]:
    if name == "done":
        return _done_action_json_schema(output_schema)
    return _ACTION_MODELS[name].model_json_schema()


def _done_action_json_schema(output_schema: dict[str, Any] | None) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "type": {"const": "done"},
        "success": {"type": "boolean"},
        "result": {"type": "string"},
    }
    required = ["type", "success", "result"]
    if output_schema is not None:
        properties["extracted_data"] = output_schema
        required.append("extracted_data")
    else:
        properties["extracted_data"] = {"type": ["object", "null"]}
    return {"type": "object", "properties": properties, "required": required}


def parse_agent_output(raw: dict[str, Any]) -> AgentOutput:
    actions = [_parse_one_action(item) for item in raw.get("action", [])]
    return AgentOutput(
        thinking=raw.get("thinking"),
        evaluation_previous_goal=raw.get("evaluation_previous_goal", ""),
        memory=raw.get("memory", ""),
        next_goal=raw.get("next_goal", ""),
        actions=actions,
    )


def _parse_one_action(item: dict[str, Any]) -> spi_actions.Action | DoneAction:
    action_type = str(item.get("type"))
    if action_type == "done":
        return DoneAction(
            success=bool(item.get("success", False)),
            result=str(item.get("result", "")),
            extracted_data=item.get("extracted_data"),
        )
    model_cls = _ACTION_MODELS.get(action_type)
    if model_cls is None:
        raise ValueError(f"model chose an unknown action type: {action_type!r}")
    return _to_spi_action(action_type, model_cls.model_validate(item))


def _to_spi_action(action_type: str, parsed: BaseModel) -> spi_actions.Action:
    """Was an 11-branch `isinstance` ladder restating field names that are
    identical on both sides. The spec knows which dataclass it builds."""

    return _SPECS_BY_NAME[action_type].from_model(parsed)  # type: ignore[no-any-return]
