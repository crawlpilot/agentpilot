"""Stage 1: what the caller wants, as a `FieldSpec` map.

`render_fields_for_prompt` turns a field map into prose for a model. This is
the inverse, and it exists so onboarding can start from *"product name, price,
sizes in stock, and all the image URLs"* rather than from hand-written JSON. A
caller who already has a v2 `fields` object skips this module entirely --
`schema.parse_fields` takes it as-is.

**The model answers in a flat shape, not in `TypeSpec`.** A `TypeSpec` is
recursive (`list` holds `items`, `table` holds `columns` which are themselves
specs) and JSON-schema-constrained structured output handles recursion badly --
models reliably emit a `list` whose `items` is a bare string, or nest one level
deeper than asked. So the model emits `shape` + `value_type` + `columns`, which
is flat, and this module builds the recursive spec. The set of shapes it can
express is exactly the set `TypeSpec` has: scalar, list, map, table.

Getting the type right is not cosmetic. `render_fields_for_prompt` feeds it
straight back to the selector agent, where telling the model a field is a `url`
or a `price` steers it toward the element or JSON path that actually carries
that value rather than the one whose rendered text merely looks right -- and
`infer_transform` keys the cleanup pipeline off the same declaration.
"""

from __future__ import annotations

import re
from typing import Any

from agentpilot.llm.client import LLMConfig, chat_json_conversation
from agentpilot.recipe.v2.schema import FieldSpec, TypeSpec
from agentpilot.recipe.v2.transform import ValueType

_VALUE_TYPES: tuple[str, ...] = (
    "string", "text", "number", "float", "integer", "price",
    "boolean", "url", "date", "datetime", "json",
)

_SHAPES: tuple[str, ...] = ("scalar", "list", "map", "table")

# A ceiling on how much one recipe is asked to collect. Past this the schema is
# almost always a misunderstanding of the request rather than a real contract,
# and every field costs a locator proposal and a verification round.
MAX_FIELDS = 40

_SYSTEM_PROMPT = """\
You turn a description of wanted data into a field list for a web-scraping \
recipe. Emit one entry per value the user asked for, and nothing they did not.

For each field choose a `shape`:
- "scalar": one value (a title, a price, a rating).
- "list": many values of the same type (all image URLs, all bullet points).
- "map": an open key -> value mapping whose KEYS come from the page and are not \
known in advance (a specifications table: "Material" -> "Cotton", "Fit" -> \
"Regular").
- "table": repeating ROWS with the SAME known columns (size + stock status per \
variant; name + price + link per search result). Give the columns.

Then choose `value_type` -- for "scalar" it types the value, for "list" it \
types each item, and for "table" each column carries its own. Prefer the \
specific type over "string": "price" for money, "url" for links and images, \
"integer"/"float" for counts and measurements, "boolean" for yes/no, \
"date"/"datetime" for times. This is what tells the next stage which element or \
JSON path actually holds the value, and how to clean it up.

Mark `required` true only for the fields that make the record worthless if \
missing -- typically the identity of the thing (its name) and the number the \
user is really after. A required field that is merely nice to have turns an \
ordinary partial result into a failed run.

Use short snake_case names. Do not invent fields the description does not ask \
for, and do not split one value into several.\
"""

_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "fields": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "shape": {"type": "string", "enum": list(_SHAPES)},
                    "value_type": {"type": "string", "enum": list(_VALUE_TYPES)},
                    "required": {"type": "boolean"},
                    "columns": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "value_type": {"type": "string", "enum": list(_VALUE_TYPES)},
                            },
                            "required": ["name"],
                        },
                    },
                },
                "required": ["name", "shape"],
            },
        }
    },
    "required": ["fields"],
}


def slugify(name: str) -> str:
    """A field name safe to use as a JSON key and a `field_names` entry.

    Field names are referenced from `field_names`, `bindings`, `repeat.row_field`
    and the emitted output object, so a name with a dot or a bracket in it reads
    as a path expression somewhere downstream.
    """

    slug = re.sub(r"[^0-9a-zA-Z]+", "_", name.strip()).strip("_").lower()
    slug = re.sub(r"_+", "_", slug)
    if not slug:
        return "field"
    if slug[0].isdigit():
        slug = f"f_{slug}"
    return slug


def _unique(name: str, taken: set[str]) -> str:
    if name not in taken:
        return name
    index = 2
    while f"{name}_{index}" in taken:
        index += 1
    return f"{name}_{index}"


def _value_type(raw: Any) -> ValueType:
    got = str(raw or "string")
    return got if got in _VALUE_TYPES else "string"  # type: ignore[return-value]


def _type_spec(item: dict[str, Any]) -> TypeSpec:
    shape = str(item.get("shape") or "scalar")
    value_type = _value_type(item.get("value_type"))

    if shape == "list":
        return TypeSpec(kind="list", items=TypeSpec(kind="scalar", value_type=value_type))
    if shape == "map":
        # An open key -> value map: `properties` stays empty because the keys
        # come from the page. `render_fields_for_prompt` renders exactly this
        # case as "open key->value map, keys come from the page".
        return TypeSpec(kind="object")
    if shape == "table":
        columns = {}
        taken: set[str] = set()
        for column in item.get("columns") or []:
            name = _unique(slugify(str(column.get("name") or "")), taken)
            taken.add(name)
            columns[name] = TypeSpec(
                kind="scalar", value_type=_value_type(column.get("value_type"))
            )
        if not columns:
            # A table with no columns can never be bound -- `validate_document`
            # rejects it outright. Degrade to a list of strings, which is at
            # least collectable, rather than emitting a schema that cannot save.
            return TypeSpec(kind="list", items=TypeSpec(kind="scalar", value_type="string"))
        return TypeSpec(kind="table", columns=columns)
    return TypeSpec(kind="scalar", value_type=value_type)


def parse_contract(raw: dict[str, Any]) -> dict[str, FieldSpec]:
    """Build the field map from the model's reply.

    A malformed entry is skipped rather than failing the batch: the other
    fields in the same reply are still what the caller asked for.
    """

    fields: dict[str, FieldSpec] = {}
    for item in raw.get("fields") or []:
        if not isinstance(item, dict):
            continue
        name = slugify(str(item.get("name") or ""))
        if not name or name in fields:
            continue
        fields[name] = FieldSpec(
            name=name,
            description=str(item.get("description") or ""),
            type=_type_spec(item),
            required=bool(item.get("required", False)),
        )
        if len(fields) >= MAX_FIELDS:
            break
    return fields


def _looks_like_json_schema(raw: dict[str, Any]) -> bool:
    return raw.get("type") == "object" and isinstance(raw.get("properties"), dict)


def _type_from_json_schema(node: dict[str, Any]) -> TypeSpec:
    """One JSON-Schema property as a `TypeSpec`."""

    kind = node.get("type")
    fmt = str(node.get("format") or "")

    if kind == "array":
        items = node.get("items") or {}
        if isinstance(items, dict) and items.get("type") == "object":
            # A list of objects is a table: repeating rows with known columns,
            # which is what a `RepeatSpec` produces.
            columns = {
                slugify(str(name)): _type_from_json_schema(prop if isinstance(prop, dict) else {})
                for name, prop in (items.get("properties") or {}).items()
            }
            if columns:
                return TypeSpec(kind="table", columns=columns)
        inner = _type_from_json_schema(items if isinstance(items, dict) else {})
        return TypeSpec(kind="list", items=inner)

    if kind == "object":
        properties = {
            slugify(str(name)): _type_from_json_schema(prop if isinstance(prop, dict) else {})
            for name, prop in (node.get("properties") or {}).items()
        }
        return TypeSpec(kind="object", properties=properties)

    if kind in ("number",):
        return TypeSpec(kind="scalar", value_type="number")
    if kind == "integer":
        return TypeSpec(kind="scalar", value_type="integer")
    if kind == "boolean":
        return TypeSpec(kind="scalar", value_type="boolean")

    # Strings carry the interesting distinctions, and `format` is where JSON
    # Schema puts them. Getting `url` or `date` right here is what steers the
    # selector agent at the element that actually holds the value.
    if fmt in ("uri", "url", "iri"):
        return TypeSpec(kind="scalar", value_type="url")
    if fmt == "date":
        return TypeSpec(kind="scalar", value_type="date")
    if fmt in ("date-time", "datetime"):
        return TypeSpec(kind="scalar", value_type="datetime")
    return TypeSpec(kind="scalar", value_type="string")


def _type_from_example(value: Any) -> TypeSpec:
    """One value from an example payload as a `TypeSpec`.

    People paste the JSON they want back far more readily than they write a
    schema for it, and the shape is right there in the example.
    """

    if isinstance(value, bool):
        return TypeSpec(kind="scalar", value_type="boolean")
    if isinstance(value, int):
        return TypeSpec(kind="scalar", value_type="integer")
    if isinstance(value, float):
        return TypeSpec(kind="scalar", value_type="number")
    if isinstance(value, list):
        first = value[0] if value else ""
        if isinstance(first, dict):
            columns = {slugify(str(k)): _type_from_example(v) for k, v in first.items()}
            if columns:
                return TypeSpec(kind="table", columns=columns)
        return TypeSpec(kind="list", items=_type_from_example(first))
    if isinstance(value, dict):
        return TypeSpec(
            kind="object",
            properties={slugify(str(k)): _type_from_example(v) for k, v in value.items()},
        )
    text = str(value or "")
    if text.startswith(("http://", "https://", "/")):
        return TypeSpec(kind="scalar", value_type="url")
    return TypeSpec(kind="scalar", value_type="string")


def fields_from_output_schema(raw: dict[str, Any]) -> dict[str, FieldSpec]:
    """The caller's desired output shape as a field map. No model involved.

    Accepts either a JSON Schema (`{"type": "object", "properties": {...}}`) or
    a plain example of the JSON they want back. Both are how people actually
    describe an output contract; neither is the v2 `fields` object, and
    requiring that one was requiring them to learn this system's vocabulary
    before they could ask it for anything.

    Deterministic on purpose. The caller stated the shape exactly, and running
    it through a model could only lose that.
    """

    if not isinstance(raw, dict) or not raw:
        return {}

    if _looks_like_json_schema(raw):
        required = {str(name) for name in (raw.get("required") or [])}
        out: dict[str, FieldSpec] = {}
        for name, prop in (raw.get("properties") or {}).items():
            node = prop if isinstance(prop, dict) else {}
            key = slugify(str(name))
            if not key or key in out:
                continue
            out[key] = FieldSpec(
                name=key,
                description=str(node.get("description") or ""),
                type=_type_from_json_schema(node),
                required=str(name) in required,
            )
            if len(out) >= MAX_FIELDS:
                break
        return out

    # A bare example object.
    example: dict[str, FieldSpec] = {}
    for name, value in raw.items():
        key = slugify(str(name))
        if not key or key in example:
            continue
        example[key] = FieldSpec(name=key, type=_type_from_example(value))
        if len(example) >= MAX_FIELDS:
            break
    return example


async def fields_from_description(
    description: str, *, llm_config: LLMConfig, url: str | None = None
) -> dict[str, FieldSpec]:
    """One structured call: a description of wanted data -> a field map.

    Raises `ValueError` when nothing usable came back, because a recipe with no
    fields is not a degraded recipe -- there is nothing for the rest of the
    pipeline to locate, and failing here says so while the caller's own words
    are still the obvious thing to correct.
    """

    from agentpilot.agent.reliability import RetryStrategy

    user = f"Wanted data:\n{description.strip()}"
    if url:
        # The URL is context for naming and typing only -- the page is not read
        # here. Recon does that, once, in the next stage.
        user += f"\n\nThese will be collected from pages like: {url}"

    raw = await RetryStrategy().execute(
        lambda: chat_json_conversation(
            [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user},
            ],
            config=llm_config,
            json_schema=_JSON_SCHEMA,
        )
    )
    fields = parse_contract(raw)
    if not fields:
        raise ValueError(
            "could not turn that description into any fields -- say what values "
            "you want collected, one per line"
        )
    return fields
