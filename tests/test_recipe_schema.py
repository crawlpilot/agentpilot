"""Pure unit tests for `agentpilot.recipe.schema` -- parsing the caller's
data contract and deriving the leaf-field/array-membership maps `build.py`
needs."""

from __future__ import annotations

from agentpilot.recipe.schema import (
    all_leaf_fields,
    leaf_to_array_map,
    parse_schema,
    render_schema_for_prompt,
)

RAW_SCHEMA = {
    "title": {"type": "scalar", "description": "product title"},
    "variants": {
        "type": "array",
        "description": "one row per size",
        "item_schema": {
            "size": {"type": "scalar", "description": "size label"},
            "price": {"type": "scalar", "description": "price for this size"},
        },
    },
}


def test_parse_schema_produces_a_field_spec_per_top_level_field() -> None:
    schema = parse_schema(RAW_SCHEMA)
    assert set(schema) == {"title", "variants"}
    assert schema["title"].type == "scalar"
    assert schema["variants"].type == "array"


def test_parse_schema_recurses_into_item_schema() -> None:
    schema = parse_schema(RAW_SCHEMA)
    assert schema["variants"].item_schema is not None
    assert set(schema["variants"].item_schema) == {"size", "price"}


def test_all_leaf_fields_flattens_array_item_schema_and_keeps_scalars() -> None:
    schema = parse_schema(RAW_SCHEMA)
    leaves = all_leaf_fields(schema)
    assert set(leaves) == {"title", "size", "price"}


def test_leaf_to_array_map_points_sub_fields_at_their_parent_array_field() -> None:
    schema = parse_schema(RAW_SCHEMA)
    mapping = leaf_to_array_map(schema)
    assert mapping == {"size": "variants", "price": "variants"}


def test_leaf_to_array_map_is_empty_when_no_array_fields() -> None:
    schema = parse_schema({"title": {"type": "scalar", "description": "d"}})
    assert leaf_to_array_map(schema) == {}


def test_render_schema_for_prompt_mentions_every_field() -> None:
    schema = parse_schema(RAW_SCHEMA)
    text = render_schema_for_prompt(schema)
    assert "title" in text
    assert "variants" in text
    assert "size" in text
    assert "price" in text
