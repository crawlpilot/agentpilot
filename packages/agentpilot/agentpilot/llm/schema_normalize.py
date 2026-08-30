"""Makes a user-written JSON Schema safe to hand to a model's structured-output
mode. Ported from Firecrawl's `normalizeSchema` / `removeDefaultProperty`
(`apps/api/src/scraper/scrapeURL/transformers/llmExtract.ts`), which exists for
exactly the reason we needed it: the schemas people actually write are not the
schemas the providers actually accept.

Three classes of breakage, all reproduced against a live endpoint before this
module existed:

1. **A root-level `{"type": "array"}`.** Both providers demand an object at the
   root -- Bedrock answers `tools.0.custom.input_schema.type: Input should be
   'object'`, OpenAI rejects a non-object `json_schema`. Wrapped as
   `{"items": <the array>}` and unwrapped again on the way out, so the caller
   still gets their array (Firecrawl does the same, down to the `items` name).
2. **A bare property map** -- `{"name": {...}, "price": {...}}` with no
   `"type"` at all. This is the single most common hand-written shape and it is
   not a schema; Bedrock answers `input_schema.type: Field required`. Wrapped
   into a real object schema.
3. **Optional fields under OpenAI's strict mode**, which requires every object
   to carry `additionalProperties: false` and to list *every* property in
   `required`. A schema with any optional field is a 400.

(3) applies only to OpenAI. Bedrock's tool `input_schema` is ordinary JSON
Schema and honours `required` as written, so strictifying there would be pure
loss -- forcing every field to be present is what makes a model invent values
for fields the page doesn't have. Hence `strict` is a parameter, not a default.

Where we improve on Firecrawl: it satisfies strict mode by marking every
property required and stopping there, which tells the model a field it cannot
find is nonetheless mandatory. We mark them required *and* widen their type
with `"null"` -- OpenAI's own documented way to express an optional field under
strict mode -- so "not on the page" stays expressible as `null` instead of
being answered with a fabrication.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

_WRAP_KEY = "items"

_SCHEMA_KEYWORDS = frozenset(
    {
        "type",
        "properties",
        "items",
        "anyOf",
        "oneOf",
        "allOf",
        "not",
        "$ref",
        "$defs",
        "definitions",
        "enum",
        "const",
        "required",
        "additionalProperties",
        "description",
        "title",
        "$schema",
        "format",
        "default",
        "nullable",
    }
)
"""Keys that mark a dict as *being* a schema rather than a map *of* schemas --
the discriminator for breakage (2)."""

_UNSUPPORTED_UNDER_STRICT = frozenset(
    {
        # global
        "default",
        # object
        "patternProperties",
        "unevaluatedProperties",
        "propertyNames",
        "minProperties",
        "maxProperties",
        # string
        "minLength",
        "maxLength",
        "pattern",
        "format",
        # number
        "minimum",
        "maximum",
        "multipleOf",
        # array
        "unevaluatedItems",
        "contains",
        "minContains",
        "maxContains",
        "minItems",
        "maxItems",
        "uniqueItems",
    }
)
"""Validation keywords OpenAI's strict mode rejects outright. Dropped rather
than passed through -- a rejected request extracts nothing, while a dropped
`minLength` costs a constraint the model was never going to enforce anyway.
Same list Firecrawl deletes in `removeDefaultProperty`."""


@dataclass(frozen=True)
class NormalizedSchema:
    schema: dict[str, Any]
    unwrap_key: str | None = None
    """Set when a root-level array was wrapped: the caller reads this key back
    out of the model's object to recover the array it asked for."""


def normalize_extraction_schema(schema: dict[str, Any], *, strict: bool) -> NormalizedSchema:
    """`schema` as the provider will accept it. `strict=True` additionally
    shapes it for OpenAI's strict structured-output mode."""

    rooted, unwrap_key = _as_object_root(schema)
    if strict:
        rooted = _strictify(rooted)
    return NormalizedSchema(schema=rooted, unwrap_key=unwrap_key)


def unwrap_result(value: Any, normalized: NormalizedSchema) -> Any:
    """Undo `_as_object_root`'s wrapping on the model's output. A model that
    ignored the wrapper and answered with the bare array is passed through --
    the wrapper is our concern, not something the caller should ever see."""

    if normalized.unwrap_key is None:
        return value
    if isinstance(value, dict) and normalized.unwrap_key in value:
        return value[normalized.unwrap_key]
    return value


def _as_object_root(schema: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    if schema.get("type") == "array":
        return (
            {
                "type": "object",
                "properties": {_WRAP_KEY: schema},
                "required": [_WRAP_KEY],
            },
            _WRAP_KEY,
        )
    if _is_property_map(schema):
        return (
            {
                "type": "object",
                "properties": dict(schema),
                "required": list(schema),
            },
            None,
        )
    if "type" not in schema and "properties" in schema:
        # Has properties but forgot to say it is an object -- the one missing
        # key is unambiguous, so supply it rather than reject the schema.
        return {**schema, "type": "object"}, None
    return schema, None


def _is_property_map(schema: dict[str, Any]) -> bool:
    """`{"name": {"type": "string"}}` -- a map of field name to field schema,
    with no schema keyword of its own. Empty dicts are not property maps (an
    empty object schema is a legitimate, if useless, thing to ask for)."""

    if not schema or _SCHEMA_KEYWORDS & schema.keys():
        return False
    return all(isinstance(value, dict) for value in schema.values())


def _strictify(node: Any) -> Any:
    if isinstance(node, list):
        return [_strictify(item) for item in node]
    if not isinstance(node, dict):
        return node

    out = {key: value for key, value in node.items() if key not in _UNSUPPORTED_UNDER_STRICT}

    for key in ("anyOf", "oneOf", "allOf"):
        if key in out:
            out[key] = [_strictify(item) for item in out[key]]
    if "not" in out:
        out["not"] = _strictify(out["not"])
    for key in ("$defs", "definitions"):
        if isinstance(out.get(key), dict):
            out[key] = {name: _strictify(sub) for name, sub in out[key].items()}

    if out.get("type") == "array" and "items" in out:
        out["items"] = _strictify(out["items"])
        return out

    if out.get("type") == "object" or "properties" in out:
        properties: dict[str, Any] = out.get("properties") or {}
        originally_required = set(out.get("required") or [])
        out["properties"] = {
            name: _nullable(_strictify(sub), optional=name not in originally_required)
            for name, sub in properties.items()
        }
        # Strict mode requires *every* property in `required`; optionality is
        # carried by the `null` union `_nullable` just added instead.
        out["required"] = list(properties)
        out["additionalProperties"] = False

    return out


def _nullable(node: Any, *, optional: bool) -> Any:
    """Widen a property's type with `"null"` so a strict-mode-required field
    can still say "the page did not have this"."""

    if not optional or not isinstance(node, dict):
        return node

    node_type = node.get("type")
    if isinstance(node_type, str):
        return {**node, "type": [node_type, "null"]} if node_type != "null" else node
    if isinstance(node_type, list):
        return node if "null" in node_type else {**node, "type": [*node_type, "null"]}
    if isinstance(node.get("anyOf"), list):
        branches = node["anyOf"]
        if any(branch.get("type") == "null" for branch in branches if isinstance(branch, dict)):
            return node
        return {**node, "anyOf": [*branches, {"type": "null"}]}
    # A `$ref`/`enum`/untyped node: `anyOf` is the only place a null can go
    # without clobbering what is there.
    return {"anyOf": [node, {"type": "null"}]}
