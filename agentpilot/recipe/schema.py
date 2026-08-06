"""The caller's data contract: a field is either `scalar` (one value) or
`array` (a repeating group of sub-fields, one row per option the build
process must find and generalize -- sizes, paginated reviews, spec-table
rows). This declared shape is what drives grouping during `build.py`'s
exploration, rather than inferring it after the fact from an undifferentiated
action trace.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class FieldSpec:
    name: str
    type: str  # "scalar" | "array"
    description: str
    item_schema: dict[str, FieldSpec] | None = None


def parse_schema(raw: dict[str, Any]) -> dict[str, FieldSpec]:
    return {name: _parse_field(name, spec) for name, spec in raw.items()}


def _parse_field(name: str, spec: dict[str, Any]) -> FieldSpec:
    field_type = spec.get("type", "scalar")
    item_schema = None
    if field_type == "array" and "item_schema" in spec:
        item_schema = parse_schema(spec["item_schema"])
    return FieldSpec(
        name=name,
        type=field_type,
        description=spec.get("description", ""),
        item_schema=item_schema,
    )


def render_schema_for_prompt(schema: dict[str, FieldSpec]) -> str:
    """A human/LLM-readable rendering of the schema, used in the exploration
    task prompt (`build.py`) and the locator-proposal prompt."""

    lines: list[str] = []
    for spec in schema.values():
        if spec.type == "array" and spec.item_schema:
            sub = ", ".join(
                f"{s.name} ({s.description})" for s in spec.item_schema.values()
            )
            lines.append(
                f"- {spec.name} (array): {spec.description}. Each row needs: {sub}. "
                "This requires finding a set of options on the page (e.g. size/color "
                "swatches, paginated items) -- interact with ONE representative option "
                "first so its data becomes visible."
            )
        else:
            lines.append(f"- {spec.name} (scalar): {spec.description}")
    return "\n".join(lines)


def leaf_to_array_map(schema: dict[str, FieldSpec]) -> dict[str, str]:
    """Maps a sub-field name (under an `array` field's `item_schema`) to its
    parent array field's name -- e.g. `{"size": "variants", "price": "variants"}`.
    Used by `build.py` to know which `FieldGroup` an newly-satisfied leaf
    field belongs to."""

    mapping: dict[str, str] = {}
    for spec in schema.values():
        if spec.type == "array" and spec.item_schema:
            for leaf_name in spec.item_schema:
                mapping[leaf_name] = spec.name
    return mapping


def all_leaf_fields(schema: dict[str, FieldSpec]) -> dict[str, FieldSpec]:
    """Flattens to the fields that actually need a `FieldLocator`: scalar
    fields as-is, and an array field's `item_schema` sub-fields (the array
    field itself is satisfied by its group's `RepeatSpec`, not a locator)."""

    leaves: dict[str, FieldSpec] = {}
    for spec in schema.values():
        if spec.type == "array" and spec.item_schema:
            leaves.update(spec.item_schema)
        else:
            leaves[spec.name] = spec
    return leaves
