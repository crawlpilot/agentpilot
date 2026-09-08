"""`agentpilot.recipe.v2.contract` -- what the caller wants, as a field map.

The output-schema path is deterministic and is the one under test here: a
caller who states the shape exactly has already said what they want, and the
conversion must not lose it.
"""

from __future__ import annotations

from agentpilot.recipe.v2.contract import (
    fields_from_output_schema,
    parse_contract,
    slugify,
)

# --- JSON Schema -------------------------------------------------------------


def test_a_json_schema_becomes_typed_fields() -> None:
    fields = fields_from_output_schema({
        "type": "object",
        "required": ["name"],
        "properties": {
            "name": {"type": "string", "description": "the product title"},
            "price": {"type": "number"},
            "in_stock": {"type": "boolean"},
            "product_url": {"type": "string", "format": "uri"},
            "released": {"type": "string", "format": "date"},
        },
    })

    assert fields["name"].required is True
    assert fields["name"].description == "the product title"
    assert fields["price"].type.value_type == "number"
    assert fields["in_stock"].type.value_type == "boolean"
    # `format` is where JSON Schema puts the distinction that steers the
    # selector agent at the element actually holding the value.
    assert fields["product_url"].type.value_type == "url"
    assert fields["released"].type.value_type == "date"
    assert fields["price"].required is False


def test_an_array_of_objects_becomes_a_table() -> None:
    """Repeating rows with known columns is exactly what a `RepeatSpec`
    produces, so it must not arrive as a list of strings."""

    fields = fields_from_output_schema({
        "type": "object",
        "properties": {
            "variants": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "size": {"type": "string"},
                        "in stock": {"type": "boolean"},
                    },
                },
            }
        },
    })

    spec = fields["variants"].type
    assert spec.kind == "table"
    assert sorted(spec.columns) == ["in_stock", "size"]
    assert spec.columns["in_stock"].value_type == "boolean"


def test_an_array_of_scalars_becomes_a_list() -> None:
    fields = fields_from_output_schema({
        "type": "object",
        "properties": {
            "images": {"type": "array", "items": {"type": "string", "format": "uri"}}
        },
    })
    spec = fields["images"].type
    assert spec.kind == "list"
    assert spec.items is not None and spec.items.value_type == "url"


# --- a plain example payload -------------------------------------------------


def test_an_example_payload_is_accepted_too() -> None:
    """People paste the JSON they want back far more readily than they write a
    schema for it, and the shape is right there in the example."""

    fields = fields_from_output_schema({
        "name": "Ribbed top",
        "price": 29.99,
        "in_stock": True,
        "images": ["https://x.test/a.jpg"],
        "sizes": [{"size": "M", "available": True}],
    })

    assert fields["name"].type.value_type == "string"
    assert fields["price"].type.value_type == "number"
    assert fields["in_stock"].type.value_type == "boolean"
    assert fields["images"].type.kind == "list"
    assert fields["images"].type.items.value_type == "url"
    assert fields["sizes"].type.kind == "table"
    assert sorted(fields["sizes"].type.columns) == ["available", "size"]


def test_a_url_in_an_example_is_recognised() -> None:
    fields = fields_from_output_schema({"link": "https://shop.test/p/1", "path": "/p/2"})
    assert fields["link"].type.value_type == "url"
    assert fields["path"].type.value_type == "url"


def test_field_names_are_made_safe() -> None:
    """Names are referenced from `field_names`, `bindings` and `row_field`, so
    one with a dot in it reads as a path expression somewhere downstream."""

    fields = fields_from_output_schema({"Product Name!": "x", "2nd price": 1})
    assert sorted(fields) == ["f_2nd_price", "product_name"]


def test_nothing_usable_yields_nothing_rather_than_guessing() -> None:
    assert fields_from_output_schema({}) == {}
    assert fields_from_output_schema({"type": "object", "properties": {}}) == {}


def test_the_description_parser_still_works() -> None:
    fields = parse_contract(
        {"fields": [{"name": "price", "shape": "scalar", "value_type": "price"}]}
    )
    assert fields["price"].type.value_type == "price"
    assert slugify("Product Name") == "product_name"
