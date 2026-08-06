"""Pure unit tests for `agentpilot.agent.actions` -- schema building and
round-trip parsing, zero browser/network."""

from __future__ import annotations

import pydantic
import pytest

from agentpilot.agent.actions import (
    DEFAULT_ALLOWED_ACTIONS,
    DoneAction,
    build_action_schema,
    parse_agent_output,
    render_action_descriptions,
)
from agentpilot.spi import actions as spi_actions


def test_default_allowed_actions_excludes_execute_js_and_tab_management() -> None:
    assert "execute_js" not in DEFAULT_ALLOWED_ACTIONS
    assert "new_tab" not in DEFAULT_ALLOWED_ACTIONS
    assert "switch_tab" not in DEFAULT_ALLOWED_ACTIONS


def test_render_action_descriptions_includes_done() -> None:
    text = render_action_descriptions()
    assert "- done:" in text
    assert "- click:" in text


def test_build_action_schema_includes_done_and_all_allowed_actions() -> None:
    schema = build_action_schema()
    action_schemas = schema["properties"]["action"]["items"]["anyOf"]
    # one schema per allowed action + one for done
    assert len(action_schemas) == len(DEFAULT_ALLOWED_ACTIONS) + 1


def test_build_action_schema_force_done_only_allows_done() -> None:
    schema = build_action_schema(force_done=True)
    action_schemas = schema["properties"]["action"]["items"]["anyOf"]
    assert len(action_schemas) == 1


def test_build_action_schema_with_output_schema_embeds_it_in_done() -> None:
    output_schema = {"type": "object", "properties": {"price": {"type": "number"}}}
    schema = build_action_schema(output_schema=output_schema)
    done_schema = next(
        s for s in schema["properties"]["action"]["items"]["anyOf"] if "success" in s["properties"]
    )
    assert done_schema["properties"]["extracted_data"] == output_schema
    assert "extracted_data" in done_schema["required"]


@pytest.mark.parametrize(
    ("raw_action", "expected_type"),
    [
        ({"type": "navigate", "url": "https://example.com"}, spi_actions.NavigateAction),
        ({"type": "go_back"}, spi_actions.GoBackAction),
        ({"type": "click", "ref": "e1"}, spi_actions.ClickAction),
        ({"type": "fill", "ref": "e1", "text": "hi"}, spi_actions.FillAction),
        ({"type": "select_option", "ref": "e1", "values": ["a"]}, spi_actions.SelectOptionAction),
        ({"type": "hover", "ref": "e1"}, spi_actions.HoverAction),
        ({"type": "press", "key": "Enter"}, spi_actions.PressAction),
        ({"type": "scroll", "direction": "down"}, spi_actions.ScrollAction),
        ({"type": "wait", "ms": 500}, spi_actions.WaitAction),
        ({"type": "extract", "format": "markdown"}, spi_actions.ExtractAction),
        ({"type": "screenshot"}, spi_actions.ScreenshotAction),
    ],
)
def test_parse_agent_output_round_trips_every_action_type(raw_action, expected_type) -> None:
    raw = {
        "evaluation_previous_goal": "ok",
        "memory": "",
        "next_goal": "keep going",
        "action": [raw_action],
    }
    output = parse_agent_output(raw)
    assert len(output.actions) == 1
    assert isinstance(output.actions[0], expected_type)


def test_parse_agent_output_done_action_not_dispatched_to_driver() -> None:
    raw = {
        "evaluation_previous_goal": "done",
        "memory": "",
        "next_goal": "",
        "action": [
            {
                "type": "done",
                "success": True,
                "result": "found it",
                "extracted_data": {"x": 1},
            }
        ],
    }
    output = parse_agent_output(raw)
    assert isinstance(output.actions[0], DoneAction)
    assert output.actions[0].success is True
    assert output.actions[0].extracted_data == {"x": 1}


def test_parse_agent_output_rejects_unknown_action_type() -> None:
    raw = {
        "evaluation_previous_goal": "",
        "memory": "",
        "next_goal": "",
        "action": [{"type": "execute_js", "script": "alert(1)"}],
    }
    with pytest.raises(ValueError, match="unknown action type"):
        parse_agent_output(raw)


def test_navigate_rejects_non_http_scheme() -> None:
    raw = {
        "evaluation_previous_goal": "",
        "memory": "",
        "next_goal": "",
        "action": [{"type": "navigate", "url": "javascript:alert(1)"}],
    }
    with pytest.raises(pydantic.ValidationError):
        parse_agent_output(raw)
