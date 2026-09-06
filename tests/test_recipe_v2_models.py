"""The v2 recipe document.

The load-bearing test here is `test_worked_examples_round_trip`: the three
example recipes in `docs/examples/recipes/` were written against three real
pages (Zara, Walmart, Amazon), and they are the only artifacts in the repo that
exercise the whole contract at once -- json_ld and hydration and css and xpath
sources, dom and json repeats, per-candidate transforms, variants, assertions,
Lua. If the dataclasses and the JSON schema ever disagree about the format,
they disagree here first.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from agentpilot.recipe.v2.models import (
    Candidate,
    ExecutionDefaults,
    FieldGroup,
    Locator,
    PageVariant,
    Predicate,
    Recipe,
    RecipeRunResult,
    RepeatSpec,
    RetryPolicy,
    Step,
    TargetSpec,
    UrlMatcher,
)
from agentpilot.recipe.v2.schema import (
    Assertion,
    FieldSpec,
    TypeSpec,
    all_leaf_fields,
    column_to_table_map,
    parse_fields,
    render_fields_for_prompt,
)

EXAMPLES = sorted((Path(__file__).parents[1] / "docs/examples/recipes").glob("*.v2.json"))


def _strip_annotations(node):
    """The examples carry `_`-prefixed author annotations (JSON has no
    comments, and the reasoning behind a candidate is worth keeping next to
    it). The schema permits them; the dataclasses ignore them."""

    if isinstance(node, dict):
        return {k: _strip_annotations(v) for k, v in node.items() if not k.startswith("_")}
    if isinstance(node, list):
        return [_strip_annotations(v) for v in node]
    return node


# --- the worked examples ----------------------------------------------------


def test_the_examples_are_actually_present() -> None:
    assert len(EXAMPLES) == 3, [p.name for p in EXAMPLES]


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.stem)
def test_worked_examples_round_trip(path: Path) -> None:
    raw = _strip_annotations(json.loads(path.read_text(encoding="utf-8")))
    recipe = Recipe.from_dict(raw, recipe_id="r1", tenant="t1")
    once = recipe.to_dict()
    twice = Recipe.from_dict(once, recipe_id="r1", tenant="t1").to_dict()
    assert once == twice, "to_dict/from_dict is not idempotent"


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.stem)
def test_worked_examples_preserve_every_candidate(path: Path) -> None:
    raw = _strip_annotations(json.loads(path.read_text(encoding="utf-8")))
    expected = sum(
        len(c) for g in raw["field_groups"] for c in g.get("bindings", {}).values()
    )
    recipe = Recipe.from_dict(raw)
    got = sum(
        len(c) for g in recipe.field_groups for c in g.bindings.values()
    )
    assert got == expected > 0


def test_zara_reads_its_size_table_from_json_not_from_clicks() -> None:
    """The contract's central claim, asserted against the real recipe: the
    entire size/price/stock table is in the page's JSON-LD `hasVariant[]`, so
    the repeat is `kind="json"` and costs no page mutation."""

    zara = next(p for p in EXAMPLES if "zara" in p.name)
    recipe = Recipe.from_dict(_strip_annotations(json.loads(zara.read_text(encoding="utf-8"))))
    group = next(g for g in recipe.field_groups if g.group_id == "size_table")
    assert group.repeat is not None
    assert group.repeat.kind == "json"
    assert group.repeat.rows_locator is not None
    assert group.repeat.rows_locator.kind == "json_ld"
    assert group.steps == []


def test_amazon_needs_xpath_because_it_has_no_structured_data() -> None:
    """Amazon ships 0 JSON-LD scripts and no hydration state, and its spec rows
    are `<tr><th>Key</th><td>Value</td></tr>` in id-less tables. Selecting a td
    by its sibling th's text is the one thing CSS cannot express."""

    amazon = next(p for p in EXAMPLES if "amazon" in p.name)
    recipe = Recipe.from_dict(_strip_annotations(json.loads(amazon.read_text(encoding="utf-8"))))
    kinds = {
        c.locator.kind
        for g in recipe.field_groups
        for cands in g.bindings.values()
        for c in cands
    }
    assert "xpath" in kinds
    assert not {"json_ld", "hydration"} & kinds


def test_uses_lua_is_computed_not_trusted() -> None:
    """`has_script` gates a stricter publication review, so it is recomputed
    rather than believed -- a flag that can drift from the thing it describes
    is worse than no flag."""

    amazon = next(p for p in EXAMPLES if "amazon" in p.name)
    recipe = Recipe.from_dict(_strip_annotations(json.loads(amazon.read_text(encoding="utf-8"))))
    assert recipe.uses_lua() is True

    walmart = next(p for p in EXAMPLES if "walmart" in p.name)
    wal = Recipe.from_dict(_strip_annotations(json.loads(walmart.read_text(encoding="utf-8"))))
    assert wal.uses_lua() is False
    assert wal.has_script is False


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.stem)
def test_hydration_paths_are_rooted_at_a_container_key(path: Path) -> None:
    """A hydration path starts at the script id, not inside the payload.

    `evaluate.py` hands `structured["hydration"]` to `resolve_path`, and
    `crawlpilot.extraction.structured_data.extract_hydration_state` keys that
    dict by script id (`__NEXT_DATA__`) or by the assigned global. A path
    beginning `props.pageProps…` therefore resolves to `None` -- silently, and
    on every field at once, because a missing key is an empty field rather
    than an error. Cheap to write, invisible until a real run, so it is pinned
    here where the resolver and the examples meet.
    """

    roots = {"__NEXT_DATA__", "__NUXT_DATA__", "__NUXT__", "__INITIAL_STATE__",
             "__APOLLO_STATE__", "__REDUX_STATE__"}
    raw = _strip_annotations(json.loads(path.read_text(encoding="utf-8")))

    def walk(node: object) -> list[str]:
        found: list[str] = []
        if isinstance(node, dict):
            if node.get("kind") == "hydration" or node.get("source") == "hydration":
                if node.get("path"):
                    found.append(str(node["path"]))
            for value in node.values():
                found += walk(value)
        elif isinstance(node, list):
            for value in node:
                found += walk(value)
        return found

    for hydration_path in walk(raw):
        head = re.split(r"[.\[]", hydration_path, maxsplit=1)[0]
        assert head in roots, f"{hydration_path!r} is not rooted at a hydration container"


# --- individual shapes ------------------------------------------------------


def test_locator_round_trips_including_nested_within() -> None:
    loc = Locator(
        kind="css",
        selector="button.size",
        within=Locator(kind="css", selector=".size-guide"),
        all=True,
        index=2,
        attribute="href",
    )
    assert Locator.from_dict(loc.to_dict()) == loc


def test_locator_to_dict_omits_defaults() -> None:
    assert Locator(kind="css", selector="h1").to_dict() == {"kind": "css", "selector": "h1"}


def test_structured_sources_are_identified() -> None:
    assert Locator(kind="json_ld", path="[0].name").is_structured
    assert not Locator(kind="css", selector="h1").is_structured


def test_step_round_trips_with_every_optional_part() -> None:
    step = Step(
        op="wait_for_selector",
        target=Locator(kind="css", selector=".dialog"),
        args={"state": "visible"},
        timeout_ms=8000,
        retry=RetryPolicy(attempts=2, backoff_ms=400),
        on_error="skip_group",
        when=[Predicate(kind="visible", selector=".btn")],
        repeat_until=Predicate(kind="count_at_least", selector=".card", n=60),
        max_repeats=20,
        label="wait for the panel",
    )
    assert Step.from_dict(step.to_dict()) == step


def test_optional_is_sugar_for_continue() -> None:
    assert Step(op="click", optional=True).effective_on_error == "continue"
    assert Step(op="click").effective_on_error == "fail"
    assert Step(op="click", on_error="skip_group").effective_on_error == "skip_group"


def test_candidate_distinguishes_absent_from_empty_transform_override() -> None:
    """`transform=None` means "use the field's pipeline"; `transform=[]` means
    "this candidate needs no cleanup". Collapsing them would silently apply the
    field's regex to a JSON-LD value that is already a clean number."""

    absent = Candidate(locator=Locator(kind="css", selector="h1"))
    empty = Candidate(locator=Locator(kind="css", selector="h1"), transform=[])
    assert "transform" not in absent.to_dict()
    assert empty.to_dict()["transform"] == []
    assert Candidate.from_dict(absent.to_dict()).transform is None
    assert Candidate.from_dict(empty.to_dict()).transform == []


def test_repeat_spec_round_trips_for_both_kinds() -> None:
    dom = RepeatSpec(
        kind="dom",
        row_field="body_measurements",
        max_iterations=12,
        option_locator=Locator(kind="css", selector="button", all=True),
        settle=Step(op="wait_for_selector", target=Locator(kind="css", selector=".active")),
    )
    js = RepeatSpec(
        kind="json",
        row_field="variants",
        max_iterations=20,
        rows_locator=Locator(kind="json_ld", path="[0].hasVariant"),
    )
    assert RepeatSpec.from_dict(dom.to_dict()) == dom
    assert RepeatSpec.from_dict(js.to_dict()) == js


def test_target_spec_is_a_guard_with_no_navigate_url() -> None:
    target = TargetSpec(match=[UrlMatcher(kind="glob", pattern="https://x.test/*")])
    assert TargetSpec.from_dict(target.to_dict()) == target
    assert "url" not in target.to_dict()


def test_recipe_round_trips_minimally() -> None:
    recipe = Recipe(
        recipe_id="r", tenant="t", name="n", version=1,
        target=TargetSpec(match=[UrlMatcher(kind="host", pattern="x.test")]),
        fields={"title": FieldSpec(name="title", type=TypeSpec(value_type="string"))},
        sample_urls=["https://x.test/a"],
        field_groups=[FieldGroup(
            group_id="g0", field_names=["title"],
            bindings={"title": [Candidate(locator=Locator(kind="css", selector="h1"))]},
        )],
    )
    assert Recipe.from_dict(recipe.to_dict()).to_dict() == recipe.to_dict()


def test_defaults_carry_the_documented_values() -> None:
    d = ExecutionDefaults()
    assert (d.step_timeout_ms, d.navigate_timeout_ms, d.max_repeat_iterations) == (
        10_000, 30_000, 20,
    )


def test_variant_round_trips() -> None:
    v = PageVariant(
        variant_id="with_size_guide", priority=10, label="x",
        detect=[Predicate(kind="selector_present", selector="#a")],
    )
    assert PageVariant.from_dict(v.to_dict()) == v


def test_run_result_treats_blocked_as_distinct_from_failed() -> None:
    assert RecipeRunResult(outcome="ok").success is True
    assert RecipeRunResult(outcome="failed").success is False
    assert RecipeRunResult(outcome="blocked").success is False
    assert RecipeRunResult(outcome="blocked").to_dict()["outcome"] == "blocked"


# --- schema helpers ---------------------------------------------------------


def test_table_fields_expand_to_their_columns_as_leaves() -> None:
    fields = parse_fields({
        "name": {"type": {"kind": "scalar", "value_type": "string"}},
        "variants": {"type": {"kind": "table", "columns": {
            "size": {"kind": "scalar", "value_type": "string"},
            "price": {"kind": "scalar", "value_type": "float"},
        }}},
    })
    leaves = all_leaf_fields(fields)
    assert set(leaves) == {"name", "size", "price"}
    assert column_to_table_map(fields) == {"size": "variants", "price": "variants"}


def test_open_object_is_rendered_as_a_page_keyed_map_for_the_model() -> None:
    fields = parse_fields({
        "specifications": {"type": {"kind": "object", "properties": {}},
                           "description": "spec sheet"},
    })
    assert "open key->value map" in render_fields_for_prompt(fields)


def test_required_and_type_hints_reach_the_prompt() -> None:
    fields = parse_fields({
        "price": {"type": {"kind": "scalar", "value_type": "price"},
                  "required": True, "description": "current price"},
    })
    rendered = render_fields_for_prompt(fields)
    assert "expected type: price" in rendered and "[REQUIRED]" in rendered


def test_field_spec_round_trips_with_transforms_and_assertions() -> None:
    spec = FieldSpec.from_dict("price", {
        "type": {"kind": "scalar", "value_type": "price"},
        "required": True,
        "transform": [{"op": "cast", "to": "float"}],
        "assertions": [{"kind": "range", "min": 0.01, "max": 1000}],
    })
    assert spec.assertions == [Assertion(kind="range", min=0.01, max=1000)]
    assert FieldSpec.from_dict("price", spec.to_dict()) == spec


def test_type_spec_round_trips_for_every_kind() -> None:
    for spec in (
        TypeSpec(kind="scalar", value_type="integer"),
        TypeSpec(kind="list", items=TypeSpec(kind="scalar", value_type="url")),
        TypeSpec(kind="object", properties={"cm": TypeSpec(value_type="float")}),
        TypeSpec(kind="table", columns={"size": TypeSpec(value_type="string")}),
    ):
        assert TypeSpec.from_dict(spec.to_dict()) == spec
