"""The caller's data contract: what to collect, in what shape, and what would
make a collected value wrong (contract 8 and 10).

Two things changed from v1 and both came from real pages. `TypeSpec` replaces
the flat `scalar | array` pair, because a caller asking for "specifications as
a key -> value map" and "product details as a list" was inexpressible before.
And `Assertion` exists because resolving is not the same as being right: on a
Walmart product page the site's own JSON carries a *sponsored competitor's*
name and price in the same document, so a locator can resolve to a perfectly
well-typed value from the wrong product and keep doing so forever. Assertions
are the cheap, model-free defence against that.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from agentpilot.recipe.v2.transform import Transform, ValueType, parse_transforms

TypeKind = Literal["scalar", "list", "object", "table"]
AssertionKind = Literal[
    "range", "matches", "in_set", "length", "not_empty",
    "cross_source_agrees", "not_equals_previous",
]


@dataclass(frozen=True)
class TypeSpec:
    kind: TypeKind = "scalar"
    value_type: ValueType = "string"
    items: TypeSpec | None = None
    properties: dict[str, TypeSpec] = field(default_factory=dict)
    columns: dict[str, TypeSpec] = field(default_factory=dict)

    @property
    def is_rows(self) -> bool:
        """A `table` field is populated by a `RepeatSpec`, not by a locator of
        its own -- the distinction `all_leaf_fields` turns on."""
        return self.kind == "table"

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.kind}
        if self.kind == "scalar":
            out["value_type"] = self.value_type
        elif self.kind == "list" and self.items is not None:
            out["items"] = self.items.to_dict()
        elif self.kind == "object":
            out["properties"] = {k: v.to_dict() for k, v in self.properties.items()}
        elif self.kind == "table":
            out["columns"] = {k: v.to_dict() for k, v in self.columns.items()}
        return out

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> TypeSpec:
        if not d:
            return cls()
        kind: TypeKind = d.get("kind", "scalar")
        return cls(
            kind=kind,
            value_type=d.get("value_type", "string"),
            items=cls.from_dict(d["items"]) if d.get("items") else None,
            properties={k: cls.from_dict(v) for k, v in (d.get("properties") or {}).items()},
            columns={k: cls.from_dict(v) for k, v in (d.get("columns") or {}).items()},
        )


@dataclass(frozen=True)
class Assertion:
    kind: AssertionKind
    min: float | None = None
    max: float | None = None
    regex: str | None = None
    values: list[Any] = field(default_factory=list)
    tolerance: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.kind}
        for name, default in (
            ("min", None), ("max", None), ("regex", None), ("values", []), ("tolerance", 0.0)
        ):
            got = getattr(self, name)
            if got != default:
                out[name] = got
        return out

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Assertion:
        return cls(
            kind=d["kind"],
            min=d.get("min"),
            max=d.get("max"),
            regex=d.get("regex"),
            values=list(d.get("values") or []),
            tolerance=float(d.get("tolerance", 0.0)),
        )


@dataclass(frozen=True)
class FieldSpec:
    name: str
    description: str = ""
    type: TypeSpec = field(default_factory=TypeSpec)
    required: bool = False
    emit_raw: bool = False
    transform: list[Transform] = field(default_factory=list)
    assertions: list[Assertion] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"type": self.type.to_dict()}
        if self.description:
            out["description"] = self.description
        if self.required:
            out["required"] = True
        if self.emit_raw:
            out["emit_raw"] = True
        if self.transform:
            out["transform"] = [t.to_dict() for t in self.transform]
        if self.assertions:
            out["assertions"] = [a.to_dict() for a in self.assertions]
        return out

    @classmethod
    def from_dict(cls, name: str, d: dict[str, Any]) -> FieldSpec:
        return cls(
            name=name,
            description=d.get("description", ""),
            type=TypeSpec.from_dict(d.get("type")),
            required=bool(d.get("required", False)),
            emit_raw=bool(d.get("emit_raw", False)),
            transform=parse_transforms(d.get("transform")),
            assertions=[Assertion.from_dict(a) for a in (d.get("assertions") or [])],
        )


def parse_fields(raw: dict[str, Any]) -> dict[str, FieldSpec]:
    return {name: FieldSpec.from_dict(name, spec) for name, spec in raw.items()}


def fields_to_dict(fields: dict[str, FieldSpec]) -> dict[str, Any]:
    return {name: spec.to_dict() for name, spec in fields.items()}


def all_leaf_fields(fields: dict[str, FieldSpec]) -> dict[str, FieldSpec]:
    """The fields that need a locator of their own.

    A `table` field is satisfied by its group's `RepeatSpec` plus one binding
    per column, so the table field itself is replaced by its columns. Everything
    else is a leaf as-is.
    """

    leaves: dict[str, FieldSpec] = {}
    for spec in fields.values():
        if spec.type.is_rows:
            for col_name, col_type in spec.type.columns.items():
                leaves[col_name] = FieldSpec(
                    name=col_name,
                    description=f"{col_name} (one per row of {spec.name})",
                    type=col_type,
                )
        else:
            leaves[spec.name] = spec
    return leaves


def column_to_table_map(fields: dict[str, FieldSpec]) -> dict[str, str]:
    """Maps a column name to its parent `table` field -- e.g.
    `{"size": "variants", "price": "variants"}`. Used to work out which group a
    newly-satisfied leaf belongs to."""

    mapping: dict[str, str] = {}
    for spec in fields.values():
        if spec.type.is_rows:
            for col_name in spec.type.columns:
                mapping[col_name] = spec.name
    return mapping


def render_fields_for_prompt(fields: dict[str, FieldSpec]) -> str:
    """A human/LLM-readable rendering of the schema, used in the exploration
    task prompt and the locator-proposal prompt.

    The type hint is not decoration: telling the model a field is a `url` or a
    `price` steers it toward the element or JSON path that actually carries
    that value, rather than the one whose rendered text merely looks right.
    """

    lines: list[str] = []
    for spec in fields.values():
        desc = spec.description or spec.name
        if spec.type.is_rows:
            cols = ", ".join(
                f"{c} ({t.value_type})" for c, t in spec.type.columns.items()
            )
            lines.append(
                f"- {spec.name} (table): {desc}. One row per option, each row needs: {cols}. "
                "If these rows are already present in the page's JSON-LD or hydration state, "
                "say so -- reading them there is far better than clicking through every option."
            )
        elif spec.type.kind == "list":
            item = spec.type.items.value_type if spec.type.items else "string"
            lines.append(f"- {spec.name} (list of {item}): {desc}")
        elif spec.type.kind == "object":
            if spec.type.properties:
                props = ", ".join(spec.type.properties)
                lines.append(f"- {spec.name} (object with {props}): {desc}")
            else:
                lines.append(
                    f"- {spec.name} (open key->value map, keys come from the page): {desc}"
                )
        else:
            vt = spec.type.value_type
            hint = "" if vt == "string" else f", expected type: {vt}"
            req = " [REQUIRED]" if spec.required else ""
            lines.append(f"- {spec.name}: {desc}{hint}{req}")
    return "\n".join(lines)
