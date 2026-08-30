"""The tool registry, and proof that collapsing three mirrors changed nothing.

Phase 6 replaced the hand-written Pydantic unions in `gateway.schemas` and
`agent.actions` -- plus `gateway.action_conversion`'s 17-entry converter table --
with projections of one `tools.ToolSpec` per verb (plan D5).

The golden files in `tests/golden/` were captured from the hand-written code
*before* it was deleted. They are the load-bearing assertion here: a refactor
that silently changed the published OpenAPI, or the schema an LLM is prompted
with, would be a behaviour change wearing a refactor's clothes.
"""

from __future__ import annotations

import ast
import json
import pathlib

import pytest
from pydantic import TypeAdapter, ValidationError

from agentpilot.agent.actions import (
    DEFAULT_ALLOWED_ACTIONS,
    build_action_schema,
    parse_agent_output,
)
from agentpilot.gateway.action_conversion import to_spi_action
from agentpilot.gateway.schemas import ActionIn
from crawlpilot.spi import actions as sa
from crawlpilot.tools import (
    CATALOG,
    DuplicateToolError,
    ToolRegistry,
    browser_tools,
    to_anthropic,
    to_mcp,
    to_openai,
)

GOLDEN = pathlib.Path(__file__).parent / "golden"


# ------------------------------------------------------------------ goldens


def test_the_wire_schema_is_unchanged() -> None:
    """The published OpenAPI must be byte-identical. Generating it is only safe
    if it produces exactly what 17 hand-written models produced."""

    golden = json.loads((GOLDEN / "wire_action_schema.json").read_text())
    generated = TypeAdapter(ActionIn).json_schema()
    assert generated == golden


def test_the_agent_schema_is_unchanged() -> None:
    """The schema an LLM is prompted with, likewise: enriching it would change
    model behaviour, which is not a refactor."""

    golden = json.loads((GOLDEN / "agent_action_schema.json").read_text())
    assert build_action_schema(DEFAULT_ALLOWED_ACTIONS) == golden


# ------------------------------------------------------------------ registry


def test_every_verb_is_registered_once() -> None:
    registry = browser_tools()
    assert len(registry) == len(CATALOG) == 17
    assert len({spec.name for spec in registry}) == 17


def test_registering_a_duplicate_raises_rather_than_overriding() -> None:
    """Browser4's `CustomToolRegistry` contract. A silent override would let an
    extension shadow a built-in verb with nothing to say so."""

    registry = browser_tools()
    with pytest.raises(DuplicateToolError):
        registry.register(CATALOG[0])


def test_a_namespace_lets_two_providers_share_a_verb() -> None:
    registry = browser_tools()
    registry.register(CATALOG[0], namespace="walmart")
    assert "browser.navigate" in registry
    assert "walmart.navigate" in registry


def test_agent_exposure_lives_with_the_verb() -> None:
    """`DEFAULT_ALLOWED_ACTIONS` used to be a separate tuple that had to be kept
    in step with the models; membership is now derived from the catalog, so a
    verb cannot be exposed in one place and forgotten in the other."""

    exposed = {name.split(".", 1)[1] for name in browser_tools().subset(agent_exposed=True).names}
    assert exposed == set(DEFAULT_ALLOWED_ACTIONS)


def test_sensitive_verbs_are_not_agent_exposed() -> None:
    registry = browser_tools()
    assert registry["browser.execute_js"].safety == "sensitive"
    assert registry["browser.execute_js"].agent_fields is None
    for tab_verb in ("new_tab", "close_tab", "switch_tab", "list_tabs"):
        assert registry[f"browser.{tab_verb}"].agent_fields is None


def test_subset_filters_by_name_and_exclusion() -> None:
    registry = browser_tools()
    assert set(registry.subset(allowed=["click", "fill"]).names) == {
        "browser.click",
        "browser.fill",
    }
    assert "browser.execute_js" not in registry.subset(exclude={"execute_js"})


# ------------------------------------------------------------------ vendor


def test_the_tools_package_imports_no_llm_sdk() -> None:
    """The registry's unit is a name, a description and a schema. An agent
    framework, an MCP server, a CLI and a plain crawler are equally first-class
    consumers, so the core must not privilege one vendor -- and the extracted
    `browserpilot` wheel must not carry an LLM SDK in its closure."""

    banned = {"anthropic", "openai", "langchain", "litellm", "cohere", "mistralai", "google"}
    offenders: list[str] = []
    tools_dir = (
        pathlib.Path(__file__).resolve().parents[1] / "packages/crawlpilot/src/crawlpilot/tools"
    )
    assert tools_dir.is_dir(), tools_dir
    for path in tools_dir.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [(node.module or "").split(".")[0]]
            else:
                continue
            offenders += [f"{path}:{node.lineno}: {n}" for n in names if n in banned]
    assert not offenders, offenders


@pytest.mark.parametrize("adapter", [to_anthropic, to_openai, to_mcp])
def test_adapters_are_pure_transforms_of_the_json_schema(adapter) -> None:  # type: ignore[no-untyped-def]
    """Every adapter must be re-shaping of our own artifact -- not a second
    source of truth with its own idea of the parameters."""

    specs = list(browser_tools().subset(agent_exposed=True))
    out = adapter(specs)
    assert len(out) == len(specs)
    for spec, entry in zip(specs, out, strict=True):
        params = (
            entry.get("input_schema")
            or entry.get("inputSchema")
            or entry["function"]["parameters"]
        )
        schema = spec.json_schema()
        # Same properties as the neutral schema, minus the discriminator the
        # provider carries out of band as the tool name.
        assert set(params["properties"]) == set(schema["properties"]) - {"type"}


def test_adapters_name_and_describe_from_the_spec() -> None:
    spec = browser_tools()["browser.click"]
    (anthropic,) = to_anthropic([spec])
    (openai,) = to_openai([spec])
    (mcp,) = to_mcp([spec])
    assert anthropic["name"] == openai["function"]["name"] == mcp["name"] == "click"
    assert anthropic["description"] == spec.description


# ------------------------------------------------------- conversion parity


def test_wire_models_convert_to_the_right_dataclass() -> None:
    """Replaces the 17-entry converter table. Field names are identical on both
    sides by construction, so there is nothing left to keep in sync."""

    adapter = TypeAdapter(ActionIn)
    cases = [
        ({"type": "navigate", "url": "https://x", "timeout_ms": 5}, sa.NavigateAction),
        ({"type": "click", "ref": "e1"}, sa.ClickAction),
        ({"type": "fill", "ref": "e1", "text": "hi"}, sa.FillAction),
        ({"type": "select_option", "ref": "e1", "values": ["a"]}, sa.SelectOptionAction),
        ({"type": "scroll", "direction": "down"}, sa.ScrollAction),
        ({"type": "execute_js", "script": "1"}, sa.ExecuteJsAction),
        ({"type": "list_tabs"}, sa.ListTabsAction),
    ]
    for payload, expected in cases:
        action = to_spi_action(adapter.validate_python(payload))
        assert isinstance(action, expected), payload

    navigate = to_spi_action(adapter.validate_python(cases[0][0]))
    assert navigate.url == "https://x"
    assert navigate.timeout_ms == 5


def test_the_agent_navigate_guard_survived() -> None:
    """A model must not be able to steer the browser to `file://` or
    `javascript:`. This validator lived on the hand-written model and is a
    security guard, so the generated one has to carry it."""

    with pytest.raises(ValueError, match="http"):
        parse_agent_output(
            {
                "evaluation_previous_goal": "",
                "memory": "",
                "next_goal": "",
                "action": [{"type": "navigate", "url": "file:///etc/passwd"}],
            }
        )


def test_the_agent_extract_surface_stays_narrow() -> None:
    """The driver and the HTTP API accept `html` and `structured_data`; the
    agent surface deliberately does not -- raw HTML burns context for no gain."""

    spec = browser_tools()["browser.extract"]
    with pytest.raises(ValidationError):
        spec.agent_model().model_validate({"type": "extract", "format": "html"})
    # ...while the wire surface still accepts it.
    assert spec.wire_model().model_validate({"type": "extract", "format": "html"})


def test_adding_a_verb_touches_one_file() -> None:
    """The acceptance criterion, asserted structurally: every surface is derived
    from `CATALOG`, so nothing outside `tools/catalog.py` enumerates verbs."""

    root = pathlib.Path(__file__).resolve().parents[1] / "packages/agentpilot/agentpilot"
    for module in ("gateway/schemas.py", "agent/actions.py"):
        source = (root / module).read_text()
        assert "class NavigateActionIn" not in source
        assert "class ClickActionIn" not in source


def test_a_registry_can_be_built_from_a_subset_without_duplicate_errors() -> None:
    registry = browser_tools()
    narrowed = registry.subset(agent_exposed=True)
    assert isinstance(narrowed, ToolRegistry)
    assert len(narrowed) == len(DEFAULT_ALLOWED_ACTIONS)
