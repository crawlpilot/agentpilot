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
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agentpilot.spi import actions as spi_actions

DEFAULT_ALLOWED_ACTIONS: tuple[str, ...] = (
    "navigate",
    "go_back",
    "click",
    "fill",
    "select_option",
    "hover",
    "press",
    "scroll",
    "wait",
    "extract",
    "screenshot",
)
"""`execute_js` and tab-management actions are excluded by default --
arbitrary JS execution is a security-sensitive capability, and tab
management adds complexity without a clear v1 need. Both remain reachable by
passing a wider `allowed_actions` tuple; neither is enabled by default."""


class NavigateActionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["navigate"]
    url: str

    @field_validator("url")
    @classmethod
    def _http_or_https_only(cls, v: str) -> str:
        if not v.startswith(("http://", "https://")):
            raise ValueError("navigate url must be an absolute http:// or https:// URL")
        return v


class GoBackActionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["go_back"]


class ClickActionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["click"]
    ref: str


class FillActionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["fill"]
    ref: str
    text: str


class SelectOptionActionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["select_option"]
    ref: str
    values: list[str] = Field(default_factory=list)


class HoverActionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["hover"]
    ref: str


class PressActionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["press"]
    key: str


class ScrollActionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["scroll"]
    direction: Literal["up", "down", "left", "right"]
    ref: str | None = None


class WaitActionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["wait"]
    ms: int | None = None


class ExtractActionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["extract"]
    format: Literal["markdown", "text"] = "markdown"


class ScreenshotActionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["screenshot"]


_ACTION_MODELS: dict[str, type[BaseModel]] = {
    "navigate": NavigateActionIn,
    "go_back": GoBackActionIn,
    "click": ClickActionIn,
    "fill": FillActionIn,
    "select_option": SelectOptionActionIn,
    "hover": HoverActionIn,
    "press": PressActionIn,
    "scroll": ScrollActionIn,
    "wait": WaitActionIn,
    "extract": ExtractActionIn,
    "screenshot": ScreenshotActionIn,
}

_ACTION_DESCRIPTIONS: dict[str, str] = {
    "navigate": "Navigate the browser to an absolute http(s) URL.",
    "go_back": "Go back to the previous page in history.",
    "click": "Click the element identified by `ref` (from the most recent page state).",
    "fill": "Fill a text input/textarea identified by `ref` with `text`.",
    "select_option": "Select one or more `values` in a <select> element identified by `ref`.",
    "hover": "Hover the mouse over the element identified by `ref`.",
    "press": "Press a keyboard key (e.g. 'Enter', 'Tab').",
    "scroll": "Scroll the page (or an element identified by `ref`) in `direction`.",
    "wait": "Wait `ms` milliseconds before the next action.",
    "extract": "Read the current page's main content as markdown or plain text.",
    "screenshot": "Capture a screenshot of the current page.",
}


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
    return _to_spi_action(model_cls.model_validate(item))


def _to_spi_action(parsed: BaseModel) -> spi_actions.Action:
    if isinstance(parsed, NavigateActionIn):
        return spi_actions.NavigateAction(url=parsed.url)
    if isinstance(parsed, GoBackActionIn):
        return spi_actions.GoBackAction()
    if isinstance(parsed, ClickActionIn):
        return spi_actions.ClickAction(ref=parsed.ref)
    if isinstance(parsed, FillActionIn):
        return spi_actions.FillAction(ref=parsed.ref, text=parsed.text)
    if isinstance(parsed, SelectOptionActionIn):
        return spi_actions.SelectOptionAction(ref=parsed.ref, values=parsed.values)
    if isinstance(parsed, HoverActionIn):
        return spi_actions.HoverAction(ref=parsed.ref)
    if isinstance(parsed, PressActionIn):
        return spi_actions.PressAction(key=parsed.key)
    if isinstance(parsed, ScrollActionIn):
        return spi_actions.ScrollAction(direction=parsed.direction, ref=parsed.ref)
    if isinstance(parsed, WaitActionIn):
        return spi_actions.WaitAction(ms=parsed.ms)
    if isinstance(parsed, ExtractActionIn):
        return spi_actions.ExtractAction(format=parsed.format)
    if isinstance(parsed, ScreenshotActionIn):
        return spi_actions.ScreenshotAction()
    raise AssertionError(f"unhandled action model: {type(parsed)!r}")
