"""The caller's data contract: a field is either `scalar` (one value) or
`array` (a repeating group of sub-fields, one row per option the build
process must find and generalize -- sizes, paginated reviews, spec-table
rows). This declared shape is what drives grouping during `build.py`'s
exploration, rather than inferring it after the fact from an undifferentiated
action trace.

Each leaf field may also carry declarative *normalization directives*
(`FieldNormalization`) describing how its raw resolved value should be cleaned
and coerced into the caller's requested format -- a structural port of the
`select -> regex/number coerce -> default` composition Pulsar expresses in
X-SQL (`str_first_float(dom_first_text(...), 0.0)` etc.). Applied by
`agentpilot.recipe.normalize` at replay/verify time; see that module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

ValueType = Literal[
    "string", "number", "integer", "price", "date", "datetime", "url", "boolean"
]
CaseMode = Literal["lower", "upper", "none"]


@dataclass
class FieldNormalization:
    """Declarative, code-free cleanup/coercion directives for one leaf field.
    Directives apply in a fixed order (see `normalize.normalize_value`):
    regex-extract -> replace -> trim/collapse_ws/case/strip_accents ->
    type-coerce -> default -> required-gate."""

    value_type: ValueType = "string"
    regex: str | None = None
    regex_group: int = 0
    replace: tuple[tuple[str, str], ...] = ()
    trim: bool = True
    collapse_ws: bool = False
    case: CaseMode = "none"
    strip_accents: bool = False
    default: Any = None
    required: bool = False
    emit_raw: bool = False

    @classmethod
    def from_spec(cls, spec: dict[str, Any]) -> FieldNormalization:
        replace_raw = spec.get("replace") or ()
        replace = tuple((str(p), str(r)) for p, r in replace_raw)
        return cls(
            value_type=spec.get("value_type", "string"),
            regex=spec.get("regex"),
            regex_group=int(spec.get("regex_group", 0)),
            replace=replace,
            trim=bool(spec.get("trim", True)),
            collapse_ws=bool(spec.get("collapse_ws", False)),
            case=spec.get("case", "none"),
            strip_accents=bool(spec.get("strip_accents", False)),
            default=spec.get("default"),
            required=bool(spec.get("required", False)),
            emit_raw=bool(spec.get("emit_raw", False)),
        )


@dataclass
class FieldSpec:
    name: str
    type: str  # "scalar" | "array"
    description: str
    item_schema: dict[str, FieldSpec] | None = None
    normalization: FieldNormalization = field(default_factory=FieldNormalization)


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
        normalization=FieldNormalization.from_spec(spec),
    )


def render_schema_for_prompt(schema: dict[str, FieldSpec]) -> str:
    """A human/LLM-readable rendering of the schema, used in the exploration
    task prompt (`build.py`) and the locator-proposal prompt."""

    lines: list[str] = []
    for spec in schema.values():
        if spec.type == "array" and spec.item_schema:
            sub = ", ".join(
                f"{s.name} ({s.description}{_type_hint(s)})" for s in spec.item_schema.values()
            )
            lines.append(
                f"- {spec.name} (array): {spec.description}. Each row needs: {sub}. "
                "This requires finding a set of options on the page (e.g. size/color "
                "swatches, paginated items) -- interact with ONE representative option "
                "first so its data becomes visible."
            )
        else:
            lines.append(f"- {spec.name} (scalar): {spec.description}{_type_hint(spec)}")
    return "\n".join(lines)


def _type_hint(spec: FieldSpec) -> str:
    """A short 'expected value type' hint appended to a field's prompt line --
    steers the model toward the element/attribute that actually carries that
    value (e.g. an href for a `url` field, a price node for a `price` field)."""

    vt = spec.normalization.value_type
    return "" if vt == "string" else f", expected type: {vt}"


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
