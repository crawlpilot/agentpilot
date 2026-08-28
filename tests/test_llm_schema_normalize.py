"""Pure unit tests for `agentpilot.llm.schema_normalize`. No network -- this
module is a pure function over a dict.

Every "breakage" case below was reproduced against a live Bedrock endpoint
before the module existed; the provider's own error message is quoted in the
test that covers it."""

from __future__ import annotations

from typing import Any

from agentpilot.llm.schema_normalize import normalize_extraction_schema, unwrap_result


def _strict(schema: dict[str, Any]):
    return normalize_extraction_schema(schema, strict=True)


def _loose(schema: dict[str, Any]):
    return normalize_extraction_schema(schema, strict=False)


# --- structural fixes (both providers) ---------------------------------


def test_root_array_is_wrapped_in_an_object() -> None:
    """`tools.0.custom.input_schema.type: Input should be 'object'`"""

    array_schema = {"type": "array", "items": {"type": "object"}}
    result = _loose(array_schema)

    assert result.schema == {
        "type": "object",
        "properties": {"items": array_schema},
        "required": ["items"],
    }
    assert result.unwrap_key == "items"


def test_unwrap_result_recovers_the_array() -> None:
    result = _loose({"type": "array", "items": {"type": "string"}})

    assert unwrap_result({"items": ["a", "b"]}, result) == ["a", "b"]


def test_unwrap_result_passes_through_a_model_that_ignored_the_wrapper() -> None:
    result = _loose({"type": "array", "items": {"type": "string"}})

    assert unwrap_result(["a", "b"], result) == ["a", "b"]


def test_unwrap_result_is_a_no_op_without_wrapping() -> None:
    result = _loose({"type": "object", "properties": {}})

    assert unwrap_result({"a": 1}, result) == {"a": 1}


def test_bare_property_map_becomes_an_object_schema() -> None:
    """`input_schema.type: Field required`"""

    result = _loose({"name": {"type": "string"}, "price": {"type": "number"}})

    assert result.schema["type"] == "object"
    assert result.schema["properties"] == {
        "name": {"type": "string"},
        "price": {"type": "number"},
    }
    assert sorted(result.schema["required"]) == ["name", "price"]
    assert result.unwrap_key is None


def test_properties_without_a_type_get_one() -> None:
    result = _loose({"properties": {"name": {"type": "string"}}})

    assert result.schema["type"] == "object"


def test_a_real_object_schema_is_left_alone_when_not_strict() -> None:
    schema = {
        "type": "object",
        "properties": {"a": {"type": "string"}},
        "required": ["a"],
    }

    assert _loose(schema).schema == schema


def test_an_empty_schema_is_not_mistaken_for_a_property_map() -> None:
    assert _loose({}).schema == {}


def test_a_composition_keyword_is_not_mistaken_for_a_property_map() -> None:
    schema = {"anyOf": [{"type": "string"}, {"type": "number"}]}

    assert _loose(schema).schema == schema


# --- strict shaping (OpenAI only) --------------------------------------


def test_strict_marks_every_property_required_and_forbids_extras() -> None:
    result = _strict(
        {
            "type": "object",
            "properties": {"a": {"type": "string"}, "b": {"type": "number"}},
            "required": ["a"],
        }
    )

    assert result.schema["additionalProperties"] is False
    assert sorted(result.schema["required"]) == ["a", "b"]


def test_strict_makes_originally_optional_properties_nullable() -> None:
    """Required-but-nullable is OpenAI's documented way to say "optional"; it
    is also the difference between a model answering `null` and inventing a
    value for a field the page does not have."""

    result = _strict(
        {
            "type": "object",
            "properties": {"a": {"type": "string"}, "b": {"type": "number"}},
            "required": ["a"],
        }
    )

    assert result.schema["properties"]["a"]["type"] == "string"
    assert result.schema["properties"]["b"]["type"] == ["number", "null"]


def test_strict_nullability_reaches_nested_objects_and_arrays() -> None:
    result = _strict(
        {
            "type": "object",
            "properties": {
                "variants": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"size": {"type": "string"}, "sku": {"type": "string"}},
                        "required": ["size"],
                    },
                }
            },
            "required": ["variants"],
        }
    )

    item = result.schema["properties"]["variants"]["items"]
    assert item["additionalProperties"] is False
    assert sorted(item["required"]) == ["size", "sku"]
    assert item["properties"]["sku"]["type"] == ["string", "null"]


def test_strict_drops_keywords_openai_rejects() -> None:
    result = _strict(
        {
            "type": "object",
            "properties": {
                "name": {"type": "string", "minLength": 1, "default": "x", "format": "uri"},
                "price": {"type": "number", "minimum": 0},
                "tags": {"type": "array", "items": {"type": "string"}, "minItems": 1},
            },
            "required": ["name", "price", "tags"],
        }
    )

    name = result.schema["properties"]["name"]
    assert "minLength" not in name and "default" not in name and "format" not in name
    assert "minimum" not in result.schema["properties"]["price"]
    assert "minItems" not in result.schema["properties"]["tags"]
    # The parts that carry meaning survive.
    assert name["type"] == "string"
    assert result.schema["properties"]["tags"]["items"] == {"type": "string"}


def test_strict_recurses_into_defs() -> None:
    result = _strict(
        {
            "type": "object",
            "properties": {"v": {"$ref": "#/$defs/V"}},
            "required": ["v"],
            "$defs": {
                "V": {
                    "type": "object",
                    "properties": {"size": {"type": "string"}, "sku": {"type": "string"}},
                    "required": ["size"],
                }
            },
        }
    )

    variant = result.schema["$defs"]["V"]
    assert variant["additionalProperties"] is False
    assert sorted(variant["required"]) == ["size", "sku"]
    assert variant["properties"]["sku"]["type"] == ["string", "null"]


def test_strict_nullable_handles_a_ref_property() -> None:
    result = _strict(
        {
            "type": "object",
            "properties": {"v": {"$ref": "#/$defs/V"}},
            "$defs": {"V": {"type": "object", "properties": {}}},
        }
    )

    assert result.schema["properties"]["v"] == {
        "anyOf": [{"$ref": "#/$defs/V"}, {"type": "null"}]
    }


def test_strict_does_not_double_add_null() -> None:
    result = _strict(
        {
            "type": "object",
            "properties": {"a": {"type": ["string", "null"]}},
        }
    )

    assert result.schema["properties"]["a"]["type"] == ["string", "null"]


def test_strict_wraps_a_root_array_then_shapes_it() -> None:
    result = _strict(
        {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"size": {"type": "string"}, "sku": {"type": "string"}},
                "required": ["size"],
            },
        }
    )

    assert result.unwrap_key == "items"
    assert result.schema["additionalProperties"] is False
    item = result.schema["properties"]["items"]["items"]
    assert sorted(item["required"]) == ["size", "sku"]
