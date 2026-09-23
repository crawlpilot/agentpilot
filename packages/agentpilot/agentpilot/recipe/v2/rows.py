"""A `table` field is ONE question, not N independent scalar questions.

`all_leaf_fields` dissolves a table into its columns so each can be located,
and by the time those columns reach `propose_and_verify` nothing is left to say
they belong to the same row. They arrive beside `title` and `price` as ordinary
scalars, and the selector prompt's "set `all` to true when the field is a list"
is then the *correct* reading of what the model was shown.

What that produced, measured on a Walmart product page against a schema
declaring `specifications: array of {name, value}`:

    "specifications": [{
      "name":  ["Product", "Skin type", "Skin care concern", ...],
      "value": ["Bodycology Toasted Sugar ... 47 reviews", ...]
    }]

One row, whose columns are parallel arrays, and whose `value` column was read
out of a recommended-products carousel rather than the specification table. Two
document-wide `all: true` selectors, resolved independently, in different
containers -- exactly the misalignment `RepeatSpec.kind="dom_rows"` was added to
prevent. `replay.py::_rows_from_dom_rows` and `PageReader.read_rows` implement
that kind, `fromPick.ts` emits it for hand-authored recipes, and the onboarding
path could not reach it: `capture.py` builds `kind="dom"` and nothing else, so a
spec table was modelled as an option set with one option to click.

So this module asks the one question that has an answer: **where are the rows,
and where inside a row is each column?**

**Verification is different here, and has to be.** `verify_locators` asks "did
this read something?", which the failure above passes with room to spare -- both
selectors read plenty. `verify_rows` reads the whole matrix and asks three
questions a document-wide selector cannot survive; see `_problems_with`.
"""

from __future__ import annotations

import json as _json
from dataclasses import dataclass, replace
from dataclasses import field as dataclass_field
from typing import Any

import structlog

from agentpilot.llm.client import LLMConfig, chat_json_conversation
from agentpilot.recipe.v2.build_trace import BuildTrace, record
from agentpilot.recipe.v2.evaluate import PageReader
from agentpilot.recipe.v2.json_index import find_row_arrays as _find_row_arrays
from agentpilot.recipe.v2.json_index import outline
from agentpilot.recipe.v2.models import Candidate, Locator, RepeatSpec
from agentpilot.recipe.v2.paths import resolve_path
from agentpilot.recipe.v2.schema import FieldSpec, TypeSpec
from agentpilot.recipe.v2.transform import Transform, TransformContext, parse_transforms

log = structlog.get_logger(__name__)

# Mirrors the caps in `selector_agent.py` -- the same page, shown for the same
# reason, so showing a different amount of it here would be arbitrary. The
# structured budget buys an *outline* rather than a prefix of the raw dump, and
# an outline is dense enough that a bigger budget is worth spending.
_MAX_STRUCTURED_CHARS = 16_000
_MAX_SNAPSHOT_CHARS = 24_000

# How many rows to read while deciding whether a proposal is any good. The
# checks below are about *shape*, and shape is settled long before row fifty.
_VERIFY_ROW_LIMIT = 50

# A column may be missing from some rows -- a spec table with an optional unit,
# a listing where not everything is on sale -- but a column missing from most of
# them was not resolved against the row.
_MIN_COLUMN_FILL = 0.5

DEFAULT_MAX_ROWS = 200

_SYSTEM_PROMPT = """\
A caller wants a TABLE of rows off this page: repeating records that all have \
the same columns. Your job is to say where the rows are and where each column \
lives INSIDE one row.

That second part is the whole point. A selector that finds every column value \
on the page is not an answer: the values then have to be zipped back together \
by position, which silently misaligns the moment one row lacks a cell, and \
which cannot tell a specification table from a recommended-products carousel \
that happens to contain similar text. Every column selector you give must be \
resolved RELATIVE TO A SINGLE ROW.

Answer with ONE of these kinds:

- "json": the rows are ALREADY an array in the page's structured data. `rows` \
is a json_ld/hydration/meta locator whose `path` points at that ARRAY, and each \
column's `path` is relative to ONE ELEMENT of it. Reach for this first. It \
costs no clicks, survives a redesign, and on many pages the table you are \
looking at was rendered from exactly this array.

- "dom_rows": N row ELEMENTS are rendered on the page. `rows` is a css or xpath \
locator matching the ROW elements -- the `<tr>`, the `<li>`, the repeated card \
-- and each column is a css/xpath locator matching ONE CELL INSIDE a row. \
Column selectors must be relative: `.//td[2]` or `[data-testid=value]`, never \
`//table//td` or a selector anchored at the document.

- "none": this page does not present this field as rows at all. Say so rather \
than forcing one; a wrong table is worse than an absent one, because it looks \
like data.

PATHS MUST BE ANCHORED from the root of the blob you were shown -- never a \
recursive or wildcard search. Pages routinely embed advertisements and \
recommendations carrying the same key names as the real content.

Set `attribute` on a css/xpath column to what you want to read: "text" (the \
default) reads textContent and DOES see collapsed content, so it works for an \
accordion body that is present but not expanded; or name a real attribute like \
"href", "src", "content".

Do NOT set `all` on a column. A column is one cell of one row.

Each entry in `columns` is FLAT: put `name` and the locator's own fields \
(`kind`, `selector` or `path`, `attribute`) side by side on the same object. Do \
not wrap the locator in a nested object.\
"""

_LOCATOR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {
            "type": "string",
            "enum": ["json_ld", "hydration", "meta", "css", "xpath"],
        },
        "path": {"type": ["string", "null"]},
        "path_lang": {"type": ["string", "null"], "enum": ["simple", "jmespath", None]},
        "selector": {"type": ["string", "null"]},
        "attribute": {"type": ["string", "null"]},
        "index": {"type": ["integer", "null"]},
    },
    "required": ["kind"],
}

# A column is FLAT -- its locator fields sit directly on it rather than in a
# nested `locator` object.
#
# `contract.py` states the rule this follows and why it exists: "JSON-schema-
# constrained structured output handles recursion badly -- models reliably emit
# a `list` whose `items` is a bare string, or nest one level deeper than asked."
# Nesting a locator inside each column broke exactly that way against a real
# Walmart page: the reply was otherwise perfect -- `kind: dom_rows`, a sensible
# rows selector, both column names right -- and every column's locator came back
# flattened, so the parser found no usable column and declined a correct answer.
_COLUMN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        **{k: v for k, v in _LOCATOR_SCHEMA["properties"].items()},
    },
    "required": ["name", "kind"],
}

_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["json", "dom_rows", "none"]},
        "rows": _LOCATOR_SCHEMA,
        "columns": {"type": "array", "items": _COLUMN_SCHEMA},
        "note": {"type": ["string", "null"]},
    },
    "required": ["kind"],
}


# The columns an open key -> value map is read through. Its keys come from the
# page, so the caller never declared any; the rows underneath are still
# name/value pairs, and `to_object` collapses them at the end.
MAP_COLUMNS = ("name", "value")


def is_open_map(spec: FieldSpec) -> bool:
    """An open key -> value map -- a specifications block, where the keys come
    from the page rather than from the caller.

    `contract.py` emits exactly this for "an open key->value map whose KEYS come
    from the page and are not known in advance". An object WITH declared
    properties is a different thing: the caller named the keys, so each is its
    own field.
    """

    return spec.type.kind == "object" and not spec.type.properties


def wanted_columns(spec: FieldSpec) -> dict[str, TypeSpec]:
    """The columns one row of this field must have."""

    if is_open_map(spec):
        return {name: TypeSpec(kind="scalar", value_type="string") for name in MAP_COLUMNS}
    return dict(spec.type.columns)


# Finding the rows already present in the page's own JSON moved to
# `json_index`, where the same walk also answers the list-field and outline
# questions -- three readers of one blob that must agree about what is in it.
# Re-exported: `find_row_arrays` is this module's published surface and the
# tests address it here.
find_row_arrays = _find_row_arrays


@dataclass
class RowBinding:
    """A verified way to produce a table field's rows."""

    repeat: RepeatSpec
    bindings: dict[str, list[Candidate]]
    rows: list[dict[str, Any]]
    """What was actually read while verifying. Nothing at build time could show
    an author the rows their recipe will produce; this is where they exist."""

    field_transform: list[Transform] = dataclass_field(default_factory=list)
    """The pipeline the FIELD carries, over the whole row set.

    `to_object` for an open map, and empty for a table. `_replay_repeat` already
    applies a repeat field's transform over its rows -- and that plumbing has
    existed, unused, because nothing at build time ever emitted the pairing. A
    specification block is rows underneath and a `{name: value}` map to the
    caller, and this is the one step between them."""

    @property
    def sample(self) -> list[dict[str, Any]]:
        return self.rows[:3]


def _parse_locator(item: Any, *, allow: tuple[str, ...]) -> Locator | None:
    if not isinstance(item, dict):
        return None
    kind = item.get("kind")
    if kind not in allow:
        return None
    if kind in ("json_ld", "hydration", "meta") and not item.get("path"):
        return None
    if kind in ("css", "xpath") and not item.get("selector"):
        return None
    return Locator(
        kind=kind,
        selector=item.get("selector"),
        index=item.get("index"),
        # Never `all`. A column is one cell of one row, and a model that sets it
        # anyway is making the exact mistake this module exists to stop.
        all=False,
        attribute=item.get("attribute") or "text",
        path=item.get("path"),
        path_lang=item.get("path_lang") or "simple",
    )


def parse_row_proposal(
    raw: dict[str, Any], spec: FieldSpec
) -> tuple[str, Locator, dict[str, Locator]] | None:
    """The model's reply as `(kind, rows_locator, columns)`, or None.

    None covers "it said none", "it said something malformed", and "it gave no
    column we asked for" -- all of which mean the same thing to the caller: this
    page does not yield this table, fall through to whatever is next.
    """

    kind = raw.get("kind")
    if kind not in ("json", "dom_rows"):
        return None

    allow = (
        ("json_ld", "hydration", "meta") if kind == "json" else ("css", "xpath")
    )
    rows = _parse_locator(raw.get("rows"), allow=allow)
    if rows is None:
        return None

    wanted = set(wanted_columns(spec))
    columns: dict[str, Locator] = {}
    for item in raw.get("columns") or []:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if name not in wanted or name in columns:
            continue
        # Flat is what the schema asks for; nested is what a model produces
        # anyway often enough that rejecting it throws away correct answers.
        # Both are accepted, flat first.
        locator = _parse_locator(item, allow=allow) or _parse_locator(
            item.get("locator"), allow=allow
        )
        if locator is not None:
            columns[name] = locator

    if not columns:
        return None
    return kind, rows, columns


async def _read_rows(
    kind: str, rows: Locator, columns: dict[str, Locator], reader: PageReader
) -> list[dict[str, Any]] | None:
    """The matrix, read the same way replay will read it.

    Deliberately the *same functions* replay uses -- `read_rows` for `dom_rows`
    and `resolve_path` per element for `json` -- rather than a build-time
    approximation of them. A verifier that reads even slightly differently from
    the engine certifies recipes that do not work.
    """

    if kind == "dom_rows":
        return await reader.read_rows(rows, columns)

    raw = await reader.read(rows)
    if not isinstance(raw, list):
        return None
    out: list[dict[str, Any]] = []
    for element in raw[:_VERIFY_ROW_LIMIT]:
        out.append({
            name: resolve_path(element, loc.path or "", loc.path_lang)
            for name, loc in columns.items()
        })
    return out


def _problems_with(rows: list[dict[str, Any]], columns: dict[str, Locator]) -> list[str]:
    """Why this proposal is not a table, or an empty list.

    Three checks, and the third is the one that matters. The first two would
    pass for the Walmart failure this module documents -- there were rows and
    the cells were full. What a document-wide selector cannot fake is
    *variation*: resolved outside its row it returns the same thing for every
    row, because it is literally the same read repeated. That is the cheapest
    available signal that a column was not scoped to a row, and it is the only
    one that catches a plausible-looking wrong answer.
    """

    if not rows:
        return ["the rows locator matched nothing"]

    problems: list[str] = []

    # Two columns reading through the SAME locator are one column reported
    # twice, whatever the values happen to look like. Checked before the
    # row-count-dependent tests below, because it does not need more than one
    # row -- and a one-row table is exactly where it slipped through: a Zara
    # build bound both `material` and `percentage` to json_ld path `value`, and
    # `cast to float` turned "100% viscose" into 100.0, so the row looked right.
    seen: dict[tuple[Any, ...], str] = {}
    for name, locator in columns.items():
        key = (locator.kind, locator.selector, locator.path, locator.path_lang,
               locator.attribute, locator.index)
        first = seen.get(key)
        if first is not None:
            problems.append(
                f"columns {first!r} and {name!r} read through the same locator, so "
                "they are one value reported twice rather than two columns"
            )
        else:
            seen[key] = name
    for name in columns:
        filled = sum(1 for row in rows if not _blank(row.get(name)))
        if filled / len(rows) < _MIN_COLUMN_FILL:
            problems.append(
                f"column {name!r} is empty in {len(rows) - filled} of {len(rows)} rows, "
                "so it is not being resolved inside each row"
            )

    if len(rows) > 1:
        varies = any(
            len({_comparable(row.get(name)) for row in rows}) > 1 for name in columns
        )
        if not varies:
            problems.append(
                f"every one of the {len(rows)} rows came back identical -- the column "
                "selectors are resolving against the whole page rather than inside a row"
            )
    return problems


def _key_in(row: dict[str, Any], name: str) -> str:
    """The row's own spelling of a column name -- `Name` where the schema says
    `name`. A path is matched case-sensitively, so the row's spelling is the one
    that resolves."""

    for key in row:
        if str(key).lower() == name.lower():
            return str(key)
    return name


def _blank(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _comparable(value: Any) -> str:
    return _json.dumps(value, sort_keys=True, default=str)


def _column_spec(spec: FieldSpec, name: str) -> FieldSpec:
    """One column as a field in its own right, so its declared type drives the
    same cleanup a top-level scalar would get."""

    return FieldSpec(
        name=name,
        description=f"{name} (one per row of {spec.name})",
        type=wanted_columns(spec).get(name, TypeSpec(kind="scalar")),
    )


def _clean(
    rows: list[dict[str, Any]],
    spec: FieldSpec,
    columns: dict[str, Locator],
    ctx: TransformContext,
) -> list[dict[str, Any]]:
    """The rows as the recipe will emit them, cell by cell.

    Through `pipeline_for` and `apply_transforms`, which is what
    `replay._resolve_columns` does -- so what the author is shown is what they
    will get, rather than the raw read that used to be all a build could offer.
    """

    from agentpilot.recipe.v2.selector_agent import pipeline_for
    from agentpilot.recipe.v2.transform import TransformError, apply_transforms

    out: list[dict[str, Any]] = []
    for row in rows:
        cleaned: dict[str, Any] = {}
        for name, locator in columns.items():
            raw = row.get(name)
            if _blank(raw):
                continue
            pipeline = pipeline_for(_column_spec(spec, name), locator)
            try:
                cleaned[name] = apply_transforms(raw, pipeline, ctx) if pipeline else raw
            except TransformError:
                # The cell was read; only the cleanup failed. Keeping the raw
                # value loses less than dropping the row, and the fill check
                # above has already had its say.
                cleaned[name] = raw
        if cleaned:
            out.append(cleaned)
    return out


async def verify_rows(
    spec: FieldSpec,
    kind: str,
    rows_locator: Locator,
    columns: dict[str, Locator],
    *,
    reader: PageReader,
    page_url: str = "",
    max_rows: int = DEFAULT_MAX_ROWS,
) -> tuple[RowBinding | None, str | None]:
    """Read the proposed table off the live page and decide whether it is one.

    Returns `(binding, None)` or `(None, why_not)`. The reason is fed back to
    the model verbatim, so it says what was read rather than that something was
    wrong -- the same discipline `verify_locators` and the judge both follow.
    """

    from agentpilot.recipe.v2.selector_agent import SOURCE_PRIORITY, infer_transform

    try:
        raw_rows = await _read_rows(kind, rows_locator, columns, reader)
    except Exception as exc:  # noqa: BLE001 - a bad selector is data, not a crash
        return None, f"reading the rows raised: {exc}"

    if raw_rows is None:
        return None, "the rows locator did not resolve to a list of rows"

    problems = _problems_with(raw_rows, columns)
    if problems:
        return None, "; ".join(problems)

    ctx = TransformContext(url=page_url, field_name=spec.name)
    cleaned = _clean(raw_rows, spec, columns, ctx)
    if not cleaned:
        return None, "the rows read fine but every cell cleaned up to nothing"

    bindings: dict[str, list[Candidate]] = {}
    for position, (name, locator) in enumerate(columns.items()):
        column_spec = _column_spec(spec, name)
        bindings[name] = [
            Candidate(
                locator=locator,
                priority=SOURCE_PRIORITY.get(locator.kind, 90) + position,
                verified_on=1,
                transform=infer_transform(column_spec, locator),
            )
        ]

    repeat = RepeatSpec(
        kind="json" if kind == "json" else "dom_rows",
        row_field=spec.name,
        max_iterations=max_rows,
        rows_locator=rows_locator,
    )

    # An open map is rows underneath and a `{name: value}` map to the caller.
    # `_replay_repeat` applies a repeat field's transform over the whole row
    # set, and `to_object` is written for exactly this -- the two have been
    # implemented and never connected, because nothing at build time emitted
    # the pairing.
    field_transform = (
        parse_transforms([{"op": "to_object", "key": "name", "value": "value"}])
        if is_open_map(spec)
        else []
    )
    return RowBinding(
        repeat=repeat, bindings=bindings, rows=cleaned, field_transform=field_transform
    ), None


def build_user_message(
    spec: FieldSpec,
    *,
    snapshot_text: str,
    structured_data: dict[str, Any],
    failure: str | None = None,
) -> str:
    columns = "\n".join(
        f"  - {name} ({column.value_type if column.kind == 'scalar' else column.kind})"
        for name, column in wanted_columns(spec).items()
    )
    parts = [
        f"Table field: {spec.name}",
        spec.description or "",
        f"Columns each row must have:\n{columns}",
        f"\nPage snapshot:\n{snapshot_text[:_MAX_SNAPSHOT_CHARS]}",
        "\nPaths available in this page's structured data (json_ld / hydration / "
        "metadata). Each path is written the way a locator takes it:\n"
        + outline(
            structured_data,
            wanted={spec.name: spec},
            max_chars=_MAX_STRUCTURED_CHARS,
        ),
    ]
    # Arrays already in the page's JSON that look like these rows, pulled out by
    # hand because the blob above is truncated and the useful part is routinely
    # past the cut. Without this the model is guessing CSS at a page it can only
    # see an accessibility tree of.
    candidates = find_row_arrays(structured_data, wanted_columns(spec), spec.name)
    if candidates:
        lines = "\n".join(
            f"- kind={kind} path={path!r} first row: "
            f"{_json.dumps(sample[0], ensure_ascii=False)[:200]}"
            for kind, path, sample in candidates
        )
        parts.append(
            "\nArrays already in this page's JSON that may be these rows. If one "
            "is right, answer kind 'json' with its path -- it costs no clicks and "
            f"survives a redesign:\n{lines}"
        )

    if not structured_data or not any(structured_data.values()):
        parts.append(
            "\nNOTE: this page publishes no usable structured data, so 'json' is "
            "not available here. Use dom_rows."
        )
    if failure:
        parts.append(
            "\nYour last answer was checked against the live page and did NOT hold "
            f"up:\n{failure}\n"
            "Propose something different. If the page really does not present this "
            "as rows, answer kind 'none'."
        )
    return "\n".join(p for p in parts if p)


async def propose_rows(
    spec: FieldSpec,
    *,
    snapshot_text: str,
    structured_data: dict[str, Any],
    reader: PageReader,
    llm_config: LLMConfig,
    page_url: str = "",
    max_rows: int = DEFAULT_MAX_ROWS,
    max_retries: int = 1,
    scope: Locator | None = None,
    trace: BuildTrace | None = None,
) -> RowBinding | None:
    """Locate a table field's rows and columns, verified against the page.

    None when this page does not present the field as rows. That is a real
    answer and often the right one -- a variant option set genuinely does need
    clicking through, and `capture.generalize_option_locator` still handles it.
    Returning a forced table instead would be worse than returning nothing,
    because a wrong table looks like data.

    `scope` is the region a person pointed at. It is attached to the ROWS
    locator only: the columns are already relative to a row, and a row is
    already inside the region. Scoping is not decoration -- a `tr` selector
    confined to `#specifications` keeps working when the page around it is
    redesigned, and stops matching a same-shaped table somewhere else entirely.
    A structured locator is never scoped, because a JSON path has no DOM
    container and attaching one would make it unresolvable.
    """

    from agentpilot.agent.reliability import RetryStrategy

    wanted = wanted_columns(spec)
    if not wanted:
        return None

    # The page's own JSON first, and without a model call. An array already
    # keyed the way the caller asked is not a guess -- and it costs no clicks,
    # survives a redesign, and is verified through the same `verify_rows` as
    # anything else, so a coincidental match cannot get through.
    for kind, path, sample in find_row_arrays(structured_data, wanted, spec.name):
        keys = {str(k).lower() for k in sample[0]}
        columns = {
            name: Locator(kind=kind, path=_key_in(sample[0], name))  # type: ignore[arg-type]
            for name in wanted
            if name.lower() in keys
        }
        if len(columns) != len(wanted):
            continue
        binding, reason = await verify_rows(
            spec, "json", Locator(kind=kind, path=path), columns,
            reader=reader, page_url=page_url, max_rows=max_rows,
        )
        if binding is not None:
            log.info("rows.bound_from_page_json", field=spec.name, kind=kind, path=path,
                     rows=len(binding.rows))
            record(
                trace, spec.name, "rows_page_json", "bound",
                locator=Locator(kind=kind, path=path),  # type: ignore[arg-type]
                read=binding.sample,
            )
            return binding
        log.info("rows.json_candidate_rejected", field=spec.name, path=path, reason=reason)
        record(
            trace, spec.name, "rows_page_json", "rejected",
            locator=Locator(kind=kind, path=path),  # type: ignore[arg-type]
            reason=reason,
        )

    failure: str | None = None
    for _attempt in range(max_retries + 1):
        user = build_user_message(
            spec,
            snapshot_text=snapshot_text,
            structured_data=structured_data,
            failure=failure,
        )
        try:
            raw = await RetryStrategy().execute(
                lambda u=user: chat_json_conversation(
                    [
                        {"role": "system", "content": _SYSTEM_PROMPT},
                        {"role": "user", "content": u},
                    ],
                    config=llm_config,
                    json_schema=_JSON_SCHEMA,
                )
            )
        except Exception as exc:  # noqa: BLE001 - the caller falls back to the dom path
            log.info("rows.proposal_failed", field=spec.name, exc_info=True)
            record(
                trace, spec.name, "rows", "rejected",
                reason=f"asking for a row proposal failed: {exc}",
            )
            return None

        if trace is not None:
            trace.exchanged("rows", [spec.name], user, raw)

        parsed = parse_row_proposal(raw, spec)
        if parsed is None:
            # `kind` alone cannot tell "the model declined" from "the model
            # answered and the answer was unusable", and those need opposite
            # responses. The reply is logged compactly so the difference is
            # visible in a build's log rather than only by re-running it.
            log.info(
                "rows.not_row_shaped",
                field=spec.name,
                kind=raw.get("kind"),
                rows=raw.get("rows"),
                columns=[c.get("name") for c in (raw.get("columns") or []) if isinstance(c, dict)],
                wanted=sorted(wanted_columns(spec)),
            )
            record(
                trace, spec.name, "rows", "rejected",
                reason=(
                    f"the model answered kind={raw.get('kind')!r} and it was not "
                    "usable as rows -- either it declined, or no column we asked "
                    f"for came back (wanted {sorted(wanted_columns(spec))})"
                ),
            )
            return None

        kind, rows_locator, columns = parsed
        if scope is not None and not rows_locator.is_structured:
            rows_locator = replace(rows_locator, within=scope)
        binding, reason = await verify_rows(
            spec, kind, rows_locator, columns,
            reader=reader, page_url=page_url, max_rows=max_rows,
        )
        if binding is not None:
            log.info(
                "rows.bound",
                field=spec.name, kind=kind, rows=len(binding.rows),
                columns=sorted(columns),
            )
            record(
                trace, spec.name, "rows", "bound",
                locator=rows_locator, read=binding.sample,
            )
            return binding
        log.info("rows.rejected", field=spec.name, kind=kind, reason=reason)
        # The three checks in `_problems_with` are the most diagnostic strings
        # this system produces -- "every one of the 10 rows came back identical"
        # names the exact failure that made a recommended-products carousel look
        # like a specification sheet. They were going to stdout.
        record(
            trace, spec.name, "rows", "rejected",
            locator=rows_locator, reason=reason,
        )
        failure = reason

    return None
