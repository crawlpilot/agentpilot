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
import dataclasses
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
from crawlpilot.wire import ActionResultWire

GOLDEN = pathlib.Path(__file__).parent / "golden"


# ------------------------------------------------------------------ goldens


def test_the_wire_schema_matches_its_golden() -> None:
    """The published OpenAPI is pinned: a verb's shape must never drift by
    accident, only by an edit to `tools/catalog.py` that also updates this."""

    golden = json.loads((GOLDEN / "wire_action_schema.json").read_text())
    generated = TypeAdapter(ActionIn).json_schema()
    assert generated == golden


def test_the_agent_schema_matches_its_golden() -> None:
    """The schema an LLM is prompted with, likewise: changing it changes model
    behaviour, so it should never move without someone meaning it to."""

    golden = json.loads((GOLDEN / "agent_action_schema.json").read_text())
    assert build_action_schema(DEFAULT_ALLOWED_ACTIONS) == golden


def test_the_response_schema_matches_its_golden() -> None:
    """The other half of the boundary, pinned the same way.

    The request union has been a projection since D5; the *response* stayed a
    hand-written mirror of `spi.actions.ActionResult` and had quietly drifted ten
    fields away from it. `crawlpilot.wire` projects it now, which removes the
    drift but makes the published shape follow the dataclass automatically --
    so it needs a golden even more than the request side did: adding a field to
    `ActionResult` is now an API change, and this is what says so in review.
    """

    golden = json.loads((GOLDEN / "wire_action_result_schema.json").read_text())
    assert ActionResultWire.model_json_schema() == golden


# The verbs a port deliberately widened, and what it added to each. Listed
# rather than waved through so that "additive" stays a claim about every *other*
# verb, and so widening another one has to be a deliberate edit here rather than
# a silently accepted diff.
_DELIBERATELY_EXTENDED = {
    "FillActionIn": {"clear"},
    "ScrollActionIn": {"pages"},
    # The agent-browser port. Both are new *filters*: absent, a snapshot behaves
    # exactly as before, so an integrator's existing request is unaffected.
    "SnapshotActionIn": {"selector", "depth"},
}


def test_the_port_only_added_to_the_pre_existing_wire_verbs() -> None:
    """Every verb that existed before the port still has exactly the shape it
    had.

    The port added seven verbs and widened two; `wire_action_schema.json` was
    regenerated to match. That regeneration is the moment a real API break could
    hide, so this checks the new schema against a snapshot of the old one taken
    before it: pre-existing verbs must be byte-identical, except the two named
    above, which may only have gained the field named there. An integrator's
    existing request keeps validating.
    """

    before = json.loads((GOLDEN / "wire_action_schema.pre_port.json").read_text())["$defs"]
    after = TypeAdapter(ActionIn).json_schema()["$defs"]

    for name, old in before.items():
        assert name in after, f"{name} disappeared from the wire union"
        new = after[name]
        added = _DELIBERATELY_EXTENDED.get(name, set())
        assert set(new["properties"]) - set(old["properties"]) == added, (
            f"{name} gained unexpected wire fields"
        )
        assert not set(old["properties"]) - set(new["properties"]), (
            f"{name} lost wire fields"
        )
        for field, schema in old["properties"].items():
            assert new["properties"][field] == schema, f"{name}.{field} changed shape"
        assert new.get("required", []) == old.get("required", []), (
            f"{name} changed which fields are required"
        )


def test_the_port_only_added_to_the_agent_action_set() -> None:
    """The agent surface grew; nothing that was offered before was taken away.

    A removed verb would silently break any caller passing an explicit
    `allowed_actions`, and would change what a model can do mid-run.
    """

    before = json.loads((GOLDEN / "agent_action_schema.pre_port.json").read_text())
    old_verbs = {
        option["properties"]["type"]["const"]
        for option in before["properties"]["action"]["items"]["anyOf"]
        if "const" in option["properties"].get("type", {})
    }
    assert old_verbs <= set(DEFAULT_ALLOWED_ACTIONS) | {"done"}


# ------------------------------------------------------------------ registry


def test_every_verb_is_registered_once() -> None:
    registry = browser_tools()
    assert len(registry) == len(CATALOG)
    # Names are unique -- the property that matters. Deliberately not a literal
    # count: pinning one makes every added verb look like a regression, and
    # `register` already raises on a duplicate.
    assert len({spec.name for spec in registry}) == len(CATALOG)


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
    """A model must not be able to run arbitrary JS, nor name a path on the
    machine the driver runs on.

    `upload_file` is the second one: `path` is read from the worker's own
    filesystem, so a model choosing it could upload anything the worker can read
    to a site it controls. Exposing it safely needs a caller-supplied allowlist,
    which does not exist yet -- until it does, the verb stays wire-only.
    """

    registry = browser_tools()
    for sensitive in ("execute_js", "upload_file"):
        assert registry[f"browser.{sensitive}"].safety == "sensitive"
        assert registry[f"browser.{sensitive}"].agent_fields is None


def test_tab_management_is_agent_exposed() -> None:
    """Ported from browser-use, which offers these to the model.

    A link that opens a new tab, a checkout that pops one, comparing two pages --
    all ordinary, and all unreachable while these were wire-only. `list_tabs`
    stays out because every observation already carries the tab list.
    """

    registry = browser_tools()
    for tab_verb in ("new_tab", "close_tab", "switch_tab"):
        assert registry[f"browser.{tab_verb}"].agent_fields is not None
    assert registry["browser.list_tabs"].agent_fields is None


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


# --------------------------------------------------- per-URL domain filtering


def _walmart_only(spec):
    return dataclasses.replace(spec, domains=("*.walmart.com",))


def test_a_domain_restricted_verb_is_offered_only_on_its_own_sites() -> None:
    """What makes the namespacing here load-bearing rather than decorative.

    `walmart.solve_wall` can be registered permanently and still cost no context
    on every other site, instead of every run paying for a verb that cannot work
    where it is.
    """

    registry = browser_tools()
    registry.register(_walmart_only(CATALOG[0]), namespace="walmart")

    on_walmart = registry.subset(page_url="https://www.walmart.com/ip/123")
    assert "walmart.navigate" in on_walmart
    assert "browser.navigate" in on_walmart, "unrestricted verbs stay available"

    elsewhere = registry.subset(page_url="https://example.test/")
    assert "walmart.navigate" not in elsewhere
    assert "browser.navigate" in elsewhere


def test_a_domain_restricted_verb_fails_closed_without_a_url() -> None:
    """A restricted verb exists because it is meaningless or unsafe elsewhere,
    so "we don't know where we are" must not be treated as permission --
    browser-use makes the same call (`tools/registry/views.py:107-111`)."""

    spec = _walmart_only(CATALOG[0])
    assert not spec.applies_to(None)
    assert not spec.applies_to("")
    assert spec.applies_to("https://cart.walmart.com/")
    # A lookalike host must not match: the pattern is anchored on the hostname,
    # not a substring of the URL.
    assert not spec.applies_to("https://walmart.com.evil.test/")


def test_an_unrestricted_verb_applies_everywhere_including_no_url() -> None:
    assert browser_tools()["browser.click"].applies_to(None)
    assert browser_tools()["browser.click"].applies_to("https://anything.test/")


# ------------------------------------------------------ the ported verb set


@pytest.mark.parametrize(
    "verb",
    ["send_keys", "find_text", "dropdown_options", "search_page", "find_elements"],
)
def test_the_ported_browser_use_verbs_reach_the_agent(verb: str) -> None:
    """The catalog was 11 agent verbs against browser-use's 24. These are the
    ones a model reaches for when a click will not do -- a shortcut, content
    below the serializer's budget, the real options of a dropdown."""

    registry = browser_tools()
    assert registry[f"browser.{verb}"].agent_fields is not None
    assert verb in DEFAULT_ALLOWED_ACTIONS


def test_every_agent_verb_builds_a_schema_and_a_dataclass() -> None:
    """A verb that cannot round-trip is worse than a missing one: the model is
    shown a tool whose output fails to parse, and burns the step finding out."""

    for spec in browser_tools().subset(agent_exposed=True):
        schema = spec.json_schema()
        assert schema["properties"]["type"]["const"] == spec.name
        required = {f for f in spec.agent_fields or () if f in schema.get("required", [])}
        payload = {"type": spec.name, **{f: _sample(schema, f) for f in required}}
        assert spec.from_model(spec.agent_model().model_validate(payload)) is not None


def _sample(schema: dict, field: str):
    prop = schema["properties"][field]
    if "const" in prop:
        return prop["const"]
    if "enum" in prop:
        return prop["enum"][0]
    kind = prop.get("type")
    if kind == "array":
        return []
    if kind == "integer":
        return 1
    if kind == "number":
        return 1.0
    if kind == "boolean":
        return True
    return "https://example.test/" if field == "url" else "x"
