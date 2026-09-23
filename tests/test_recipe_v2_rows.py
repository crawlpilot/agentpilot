"""`agentpilot.recipe.v2.rows` -- a table located as rows, not as N scalars.

The thing under test is `verify_rows`, and specifically whether it can tell a
real table from the failure that motivated this module: two document-wide
`all: true` selectors, resolved independently, zipped into one row. That
proposal reads plenty and passes every "did it resolve?" check ever written,
which is exactly why the checks here are about SHAPE.

No browser and no model -- the reader is faked, so what is exercised is the
accept/reject decision and nothing else.
"""

from __future__ import annotations

from typing import Any

from agentpilot.recipe.v2 import rows as rows_mod
from agentpilot.recipe.v2.models import Locator
from agentpilot.recipe.v2.rows import (
    _problems_with,
    find_row_arrays,
    is_open_map,
    parse_row_proposal,
    propose_rows,
    verify_rows,
    wanted_columns,
)
from agentpilot.recipe.v2.schema import FieldSpec, TypeSpec

SPECS = FieldSpec(
    name="specifications",
    description="the specification table",
    type=TypeSpec(
        kind="table",
        columns={
            "name": TypeSpec(kind="scalar", value_type="string"),
            "value": TypeSpec(kind="scalar", value_type="string"),
        },
    ),
)

# What the page actually holds: five specification rows.
REAL_ROWS = [
    {"name": "Brand", "value": "Bodycology"},
    {"name": "Form", "value": "Cream"},
    {"name": "Skin type", "value": "All"},
    {"name": "Scent", "value": "Pink Vanilla"},
    {"name": "Size", "value": "8 oz"},
]


class _Reader:
    """Stands in for `PageReader`.

    `rows` is what `read_rows` returns (the `dom_rows` path) and `json` is what
    `read` returns for a structured locator (the `json` path).
    """

    def __init__(self, *, rows: Any = None, json: Any = None) -> None:
        self.rows = rows
        self.json = json
        self.read_rows_calls: list[dict[str, Locator]] = []

    async def read_rows(self, rows_locator: Locator, columns: dict[str, Locator]):
        self.read_rows_calls.append(dict(columns))
        return self.rows

    async def read(self, locator: Locator):
        return self.json


def _cols(**selectors: str) -> dict[str, Locator]:
    return {n: Locator(kind="css", selector=s) for n, s in selectors.items()}


async def _verify(reader, kind="dom_rows", rows=None, columns=None):
    return await verify_rows(
        SPECS,
        kind,
        rows or Locator(kind="css", selector="tr"),
        columns or _cols(name="th", value="td"),
        reader=reader,  # type: ignore[arg-type]
        page_url="https://x.test/p/1",
    )


# --- the check that matters --------------------------------------------------


async def test_columns_resolved_against_the_whole_page_are_rejected() -> None:
    """THE failure. Two document-wide selectors read the right *values* and put
    them in the wrong *place*: every row comes back identical, because each
    column is literally the same read repeated. Nothing about "did this
    resolve?" can see that, which is why it reached production as
    `[{"name": [...5...], "value": [...5...]}]`."""

    same_every_row = [{"name": "Brand", "value": "Bodycology"}] * 5
    binding, reason = await _verify(_Reader(rows=same_every_row))

    assert binding is None
    assert reason is not None
    assert "whole page" in reason and "5 rows" in reason


async def test_a_real_table_is_accepted_with_its_rows() -> None:
    binding, reason = await _verify(_Reader(rows=REAL_ROWS))

    assert reason is None
    assert binding is not None
    assert binding.repeat.kind == "dom_rows"
    assert binding.repeat.row_field == "specifications"
    assert sorted(binding.bindings) == ["name", "value"]
    # The rows as the recipe will emit them -- the shape the caller declared.
    assert binding.rows[0] == {"name": "Brand", "value": "Bodycology"}
    assert len(binding.rows) == 5


async def test_a_column_missing_from_most_rows_is_rejected() -> None:
    """A column empty in four rows of five was not resolved inside the row. One
    or two gaps are ordinary -- an optional unit, a listing where not everything
    is on sale -- so the bar is the majority, not perfection."""

    patchy = [{"name": r["name"], "value": None} for r in REAL_ROWS]
    patchy[0]["value"] = "Bodycology"
    binding, reason = await _verify(_Reader(rows=patchy))

    assert binding is None
    assert reason is not None and "'value'" in reason


async def test_a_column_with_a_few_gaps_is_still_a_table() -> None:
    gappy = [dict(r) for r in REAL_ROWS]
    gappy[4]["value"] = None
    binding, _ = await _verify(_Reader(rows=gappy))

    assert binding is not None
    # The row with nothing in it keeps its other cell rather than being dropped.
    assert binding.rows[4] == {"name": "Size"}


async def test_no_rows_at_all_is_reported_as_such() -> None:
    binding, reason = await _verify(_Reader(rows=[]))
    assert binding is None
    assert reason is not None and "matched nothing" in reason


async def test_a_single_row_page_is_not_rejected_for_lack_of_variation() -> None:
    """The variation check needs two rows to mean anything. A table that
    genuinely has one row must not be refused for it."""

    binding, _ = await _verify(_Reader(rows=[REAL_ROWS[0]]))
    assert binding is not None
    assert len(binding.rows) == 1


# --- reading it out of the page's own JSON -----------------------------------


async def test_json_rows_resolve_each_column_against_its_own_element() -> None:
    """The one to reach for: no clicks, and it survives a redesign that
    destroys every class name."""

    array = [
        {"label": "Brand", "text": "Bodycology"},
        {"label": "Form", "text": "Cream"},
    ]
    binding, reason = await verify_rows(
        SPECS,
        "json",
        Locator(kind="hydration", path="props.specs"),
        {
            "name": Locator(kind="hydration", path="label"),
            "value": Locator(kind="hydration", path="text"),
        },
        reader=_Reader(json=array),  # type: ignore[arg-type]
        page_url="https://x.test/p/1",
    )

    assert reason is None
    assert binding is not None
    assert binding.repeat.kind == "json"
    assert binding.rows == [
        {"name": "Brand", "value": "Bodycology"},
        {"name": "Form", "value": "Cream"},
    ]


async def test_a_json_path_that_is_not_an_array_is_rejected() -> None:
    binding, reason = await verify_rows(
        SPECS,
        "json",
        Locator(kind="hydration", path="props.specs"),
        {"name": Locator(kind="hydration", path="label")},
        reader=_Reader(json={"label": "Brand"}),  # type: ignore[arg-type]
        page_url="",
    )
    assert binding is None
    assert reason is not None and "list of rows" in reason


# --- cleanup runs per cell, as replay runs it --------------------------------


async def test_each_cell_is_cleaned_by_its_own_declared_type() -> None:
    """A column's type drives its pipeline, cell by cell, through the same
    `pipeline_for` a top-level scalar goes through -- so what the author is
    shown at build time is what the recipe will produce."""

    priced = FieldSpec(
        name="variants",
        type=TypeSpec(
            kind="table",
            columns={
                "size": TypeSpec(kind="scalar", value_type="string"),
                "price": TypeSpec(kind="scalar", value_type="price"),
            },
        ),
    )
    binding, reason = await verify_rows(
        priced,
        "dom_rows",
        Locator(kind="css", selector="li"),
        _cols(size=".size", price=".price"),
        reader=_Reader(rows=[  # type: ignore[arg-type]
            {"size": " S ", "price": "₹ 9,550.00"},
            {"size": " M ", "price": "₹ 8,200.00"},
        ]),
        page_url="",
    )

    assert reason is None
    assert binding is not None
    assert binding.rows == [
        {"size": "S", "price": 9550.0},
        {"size": "M", "price": 8200.0},
    ]


# --- finding the rows in the page's own JSON ---------------------------------

# The shape a real Walmart page has: three sibling arrays, all
# `[{name, value}]`, all of which verify. Only one is the specification sheet.
WALMART_JSON = {
    "hydration": {
        "__NEXT_DATA__": {"props": {"pageProps": {"initialData": {"data": {"idml": {
            "indications": [
                {"name": "Stop Use Indications", "value": "DISCONTINUE IF RASH"},
                {"name": "Warnings", "value": "External use only"},
            ],
            "specifications": [
                {"name": "Primary ingredient", "value": "Shea butter"},
                {"name": "Brand", "value": "Bodycology"},
            ],
            "productHighlights": [
                {"name": "Skin type", "value": "All", "iconURL": None},
                {"name": "Form", "value": "Cream", "iconURL": None},
            ],
        }}}}}},
    },
    "json_ld": [],
    "metadata": {},
}


def test_the_rows_are_found_in_the_pages_json_without_a_model() -> None:
    """The best possible answer used to be invisible. The blob shown to the
    model is capped at 12k and Walmart's `__NEXT_DATA__` is 600k, so the spec
    rows -- keyed exactly as the caller asked -- never appeared in the prompt at
    all, and the model was left guessing CSS at a page containing no `<table>`
    and no `<tr>`."""

    got = find_row_arrays(WALMART_JSON, wanted_columns(SPECS), "specifications")

    assert got
    kind, path, sample = got[0]
    assert kind == "hydration"
    assert path.endswith(".specifications")
    assert sample[0] == {"name": "Primary ingredient", "value": "Shea butter"}


def test_the_sites_own_name_for_an_array_breaks_the_tie() -> None:
    """All three arrays are `[{name, value}]` and all three would verify, so
    without this the first one found wins -- which is the plausible-but-wrong
    answer this whole module exists to avoid."""

    for field_name, expected in [
        ("specifications", ".specifications"),
        ("product_highlights", ".productHighlights"),
        ("indications", ".indications"),
    ]:
        spec = FieldSpec(
            name=field_name,
            type=TypeSpec(kind="table", columns=dict(SPECS.type.columns)),
        )
        top = find_row_arrays(WALMART_JSON, wanted_columns(spec), field_name)[0]
        assert top[1].endswith(expected), f"{field_name} -> {top[1]}"


def test_an_array_that_shares_only_one_key_ranks_below_a_real_match() -> None:
    data = {
        "hydration": {"d": {
            "breadCrumbs": [{"name": "Personal Care", "url": "/cp/1"},
                            {"name": "Body", "url": "/cp/2"}],
            "specs": [{"name": "Brand", "value": "X"}, {"name": "Form", "value": "Y"}],
        }},
        "json_ld": [], "metadata": {},
    }
    got = find_row_arrays(data, wanted_columns(SPECS), "specifications")
    assert got[0][1].endswith(".specs")


def test_nothing_row_shaped_in_the_json_finds_nothing() -> None:
    assert find_row_arrays({"hydration": {"a": {"b": 1}}, "json_ld": [], "metadata": {}},
                           wanted_columns(SPECS), "specifications") == []
    assert find_row_arrays(WALMART_JSON, {}, "specifications") == []


async def test_a_json_match_is_bound_without_asking_a_model(monkeypatch) -> None:
    """Deterministic first: an array already keyed the way the caller asked is
    not a guess. It still goes through `verify_rows`, so a coincidental match
    cannot get through -- but no model call is spent on it."""

    async def never(*a, **k):
        raise AssertionError("the model should not have been asked")

    monkeypatch.setattr(rows_mod, "chat_json_conversation", never)

    rows = WALMART_JSON["hydration"]["__NEXT_DATA__"]["props"]["pageProps"][
        "initialData"]["data"]["idml"]["specifications"]

    binding = await propose_rows(
        SPECS,
        snapshot_text="",
        structured_data=WALMART_JSON,
        reader=_Reader(json=rows),  # type: ignore[arg-type]
        llm_config=None,
    )

    assert binding is not None
    assert binding.repeat.kind == "json"
    assert binding.repeat.rows_locator is not None
    assert binding.repeat.rows_locator.path.endswith(".specifications")
    assert binding.rows[0] == {"name": "Primary ingredient", "value": "Shea butter"}


# --- an open key -> value map ------------------------------------------------

SPEC_MAP = FieldSpec(
    name="specifications",
    description="the specification block",
    type=TypeSpec(kind="object"),
)


def test_an_open_map_is_rows_underneath() -> None:
    """`contract.py` emits `object` with no properties for "a map whose KEYS
    come from the page". Asked for as a scalar it can only ever return the
    block's own heading -- which is precisely what a Walmart build returned.
    Underneath it is name/value rows, like any other table."""

    assert is_open_map(SPEC_MAP)
    assert sorted(wanted_columns(SPEC_MAP)) == ["name", "value"]

    # An object whose keys the CALLER named is a different thing: each is its
    # own field, not a row.
    declared = FieldSpec(
        name="dims",
        type=TypeSpec(kind="object", properties={"w": TypeSpec(), "h": TypeSpec()}),
    )
    assert not is_open_map(declared)


async def test_a_map_is_bound_as_rows_carrying_to_object() -> None:
    """The pairing that has existed on both sides and never been connected:
    `_replay_repeat` applies a repeat field's transform over its rows, and
    `to_object` collapses `[{name, value}, ...]` into `{name: value}`. Nothing
    at build time emitted it, so a map could not be produced at all."""

    binding, reason = await verify_rows(
        SPEC_MAP,
        "dom_rows",
        Locator(kind="css", selector="tr"),
        _cols(name="th", value="td"),
        reader=_Reader(rows=REAL_ROWS),  # type: ignore[arg-type]
        page_url="",
    )

    assert reason is None
    assert binding is not None
    assert binding.repeat.kind == "dom_rows"
    assert binding.repeat.row_field == "specifications"
    assert [t.op for t in binding.field_transform] == ["to_object"]

    # ...and running that pipeline over the rows gives the caller's shape.
    from agentpilot.recipe.v2.transform import TransformContext, apply_transforms

    got = apply_transforms(binding.rows, binding.field_transform, TransformContext())
    assert got == {
        "Brand": "Bodycology",
        "Form": "Cream",
        "Skin type": "All",
        "Scent": "Pink Vanilla",
        "Size": "8 oz",
    }


async def test_a_table_carries_no_reshape() -> None:
    """A caller who declared `array of {name, value}` asked for rows and gets
    rows. Only a map is collapsed."""

    binding, _ = await _verify(_Reader(rows=REAL_ROWS))
    assert binding is not None
    assert binding.field_transform == []


# --- parsing the model's reply -----------------------------------------------


def test_all_is_never_honoured_on_a_column() -> None:
    """A column is one cell of one row. A model that sets `all` anyway is making
    the exact mistake this module exists to stop, so it is not passed through --
    the alternative is a locator that reads a whole set into one cell."""

    parsed = parse_row_proposal(
        {
            "kind": "dom_rows",
            "rows": {"kind": "css", "selector": "tr"},
            "columns": [{"name": "name", "locator": {
                "kind": "css", "selector": "th", "all": True,
            }}],
        },
        SPECS,
    )
    assert parsed is not None
    _kind, _rows, columns = parsed
    assert columns["name"].all is False


def test_a_flat_column_is_the_shape_asked_for() -> None:
    """`contract.py` states the rule: structured output handles nesting badly.
    A nested `locator` object broke exactly that way against a real Walmart
    page -- the reply was otherwise perfect and every column arrived flattened,
    so a correct answer was declined."""

    parsed = parse_row_proposal(
        {
            "kind": "dom_rows",
            "rows": {"kind": "css", "selector": "table tbody tr"},
            "columns": [
                {"name": "name", "kind": "css", "selector": "th"},
                {"name": "value", "kind": "css", "selector": "td", "attribute": "text"},
            ],
        },
        SPECS,
    )
    assert parsed is not None
    _kind, rows, columns = parsed
    assert rows.selector == "table tbody tr"
    assert columns["name"].selector == "th"
    assert columns["value"].selector == "td"


def test_a_nested_column_is_accepted_too() -> None:
    """Both shapes, because rejecting either throws away a correct answer for a
    reason the caller cannot see or fix."""

    parsed = parse_row_proposal(
        {
            "kind": "dom_rows",
            "rows": {"kind": "css", "selector": "tr"},
            "columns": [{"name": "name", "locator": {"kind": "css", "selector": "th"}}],
        },
        SPECS,
    )
    assert parsed is not None
    assert parsed[2]["name"].selector == "th"


def test_a_kind_of_none_is_a_real_answer() -> None:
    """"This page does not present it as rows" is useful and honest. A forced
    table is worse than no table, because it looks like data."""

    assert parse_row_proposal({"kind": "none"}, SPECS) is None


def test_a_column_the_schema_never_declared_is_dropped() -> None:
    parsed = parse_row_proposal(
        {
            "kind": "dom_rows",
            "rows": {"kind": "css", "selector": "tr"},
            "columns": [
                {"name": "name", "locator": {"kind": "css", "selector": "th"}},
                {"name": "invented", "locator": {"kind": "css", "selector": "td"}},
            ],
        },
        SPECS,
    )
    assert parsed is not None
    assert sorted(parsed[2]) == ["name"]


def test_a_json_kind_may_not_carry_a_css_rows_locator() -> None:
    """The kinds are not interchangeable: `_rows_from_json` reads an array out
    of structured data and would silently return nothing for a css locator."""

    assert parse_row_proposal(
        {"kind": "json", "rows": {"kind": "css", "selector": "tr"},
         "columns": [{"name": "name", "locator": {"kind": "css", "selector": "th"}}]},
        SPECS,
    ) is None


def test_a_proposal_with_no_usable_column_is_nothing() -> None:
    assert parse_row_proposal(
        {"kind": "dom_rows", "rows": {"kind": "css", "selector": "tr"}, "columns": []},
        SPECS,
    ) is None


# --- the loop around it ------------------------------------------------------


class _StubLLM:
    def __init__(self, replies: list[dict[str, Any]]) -> None:
        self.replies = replies
        self.prompts: list[str] = []

    async def __call__(self, messages, **kwargs):
        self.prompts.append(messages[1]["content"])
        return self.replies.pop(0)


async def test_a_rejected_proposal_is_fed_back_and_the_retry_can_fix_it(
    monkeypatch,
) -> None:
    """The reason says what was read, not that something was wrong, so the
    second attempt is told what to avoid rather than merely asked again."""

    stub = _StubLLM([
        {"kind": "dom_rows", "rows": {"kind": "css", "selector": ".spec"},
         "columns": [
             {"name": "name", "locator": {"kind": "css", "selector": ".k", "all": True}},
             {"name": "value", "locator": {"kind": "css", "selector": ".v", "all": True}},
         ]},
        {"kind": "dom_rows", "rows": {"kind": "css", "selector": "tr"},
         "columns": [
             {"name": "name", "locator": {"kind": "css", "selector": "th"}},
             {"name": "value", "locator": {"kind": "css", "selector": "td"}},
         ]},
    ])
    monkeypatch.setattr(rows_mod, "chat_json_conversation", stub)

    class _TwoAnswers:
        def __init__(self) -> None:
            self.n = 0

        async def read_rows(self, rows_locator, columns):
            self.n += 1
            return [{"name": "Brand", "value": "Bodycology"}] * 5 if self.n == 1 else REAL_ROWS

        async def read(self, locator):
            return None

    binding = await propose_rows(
        SPECS,
        snapshot_text="<table>…</table>",
        structured_data={},
        reader=_TwoAnswers(),  # type: ignore[arg-type]
        llm_config=None,
        page_url="https://x.test/p/1",
    )

    assert binding is not None
    assert binding.rows == REAL_ROWS
    assert "did NOT hold up" in stub.prompts[1]
    assert "whole page" in stub.prompts[1]


async def test_a_proposal_that_never_holds_up_returns_nothing(monkeypatch) -> None:
    """Not an error. The caller falls back to the click-through path, which is
    the right answer for a genuine variant option set."""

    reply = {
        "kind": "dom_rows", "rows": {"kind": "css", "selector": "tr"},
        "columns": [{"name": "name", "locator": {"kind": "css", "selector": "th"}}],
    }
    monkeypatch.setattr(rows_mod, "chat_json_conversation", _StubLLM([reply, reply]))

    binding = await propose_rows(
        SPECS,
        snapshot_text="",
        structured_data={},
        reader=_Reader(rows=[]),  # type: ignore[arg-type]
        llm_config=None,
    )
    assert binding is None


async def test_a_model_outage_is_not_an_error_either(monkeypatch) -> None:
    async def boom(*a, **k):
        raise RuntimeError("503 from the model")

    monkeypatch.setattr(rows_mod, "chat_json_conversation", boom)

    assert await propose_rows(
        SPECS,
        snapshot_text="",
        structured_data={},
        reader=_Reader(),  # type: ignore[arg-type]
        llm_config=None,
    ) is None


async def test_a_table_with_no_columns_is_never_asked_about() -> None:
    """`contract.py` degrades a column-less table to a list, so this should not
    arise -- but asking the model to find rows with no columns in them would
    burn a call to learn nothing."""

    empty = FieldSpec(name="t", type=TypeSpec(kind="table"))
    assert await propose_rows(
        empty,
        snapshot_text="",
        structured_data={},
        reader=_Reader(),  # type: ignore[arg-type]
        llm_config=None,
    ) is None


def test_the_prompt_states_the_rule_the_failure_broke() -> None:
    assert "RELATIVE TO A SINGLE ROW" in rows_mod._SYSTEM_PROMPT
    assert "Do NOT set `all` on a column" in rows_mod._SYSTEM_PROMPT


def test_two_columns_reading_the_same_locator_are_one_column_twice() -> None:
    """Seen in a real Zara build: `material` and `percentage` were BOTH bound to
    json_ld path `value`, and `cast to float` turned "100% viscose" into 100.0 --
    so the single row looked entirely plausible.

    The variance check below cannot catch it: with one row there is nothing to
    vary. This check does not depend on the row count, because two columns
    reading through the same locator are wrong however many rows there are.
    """

    same = Locator(kind="json_ld", path="value")
    problems = _problems_with(
        [{"material": "100% viscose", "percentage": "100% viscose"}],
        {"material": same, "percentage": same},
    )
    assert problems
    assert "same locator" in problems[0]
    assert "one value reported twice" in problems[0]


def test_columns_reading_different_paths_are_fine() -> None:
    problems = _problems_with(
        [{"name": "Brand", "value": "Zara"}, {"name": "Fit", "value": "Regular"}],
        {"name": Locator(kind="json_ld", path="name"),
         "value": Locator(kind="json_ld", path="value")},
    )
    assert problems == []


# --- columns the page does not have ------------------------------------------
#
# Straight from a real Zara build's own trace. `images` was asked for as
# `{url, alt_text, position}`; the page's JSON-LD `[0].image` carries only URLs.
# `url` resolved in all 9 rows and the other two in none, and the partial-fill
# check then rejected the WHOLE proposal -- sixteen times in a row, the same
# rows locator each time. The field ended up unbound and the build died in
# `validate_document`:
#
#   field_groups.group-2-4242d7.images.alt_text: column "alt_text" has no
#   candidates bound; ... images.position: column "position" has no candidates
#
# The columns were never on the page. `contract.py` derives them from what the
# caller ASKED for, so no locator could ever have filled them.

IMAGES = FieldSpec(
    name="images",
    description="every product image",
    type=TypeSpec(
        kind="table",
        columns={
            "url": TypeSpec(kind="scalar", value_type="url"),
            "alt_text": TypeSpec(kind="scalar", value_type="string"),
            "position": TypeSpec(kind="scalar", value_type="integer"),
        },
    ),
)

# What `[0].image` actually yields: a URL per row, and nothing for the two
# columns the caller invented.
IMAGE_ROWS = [
    {"url": f"https://static.zara.net/photos/{i}.jpg", "alt_text": None, "position": None}
    for i in range(9)
]


async def _verify_images(reader, columns):
    return await verify_rows(
        IMAGES,
        "json",
        Locator(kind="json_ld", path="[0].image"),
        columns,
        reader=reader,  # type: ignore[arg-type]
        page_url="https://www.zara.com/in/en/p08004856.html",
    )


def _image_cols():
    return {
        "url": Locator(kind="json_ld", path="url"),
        "alt_text": Locator(kind="json_ld", path="alt_text"),
        "position": Locator(kind="json_ld", path="position"),
    }


async def test_a_column_the_page_cannot_fill_does_not_sink_the_ones_it_can() -> None:
    binding, reason = await _verify_images(_Reader(json=IMAGE_ROWS), _image_cols())

    assert reason is None
    assert binding is not None
    assert set(binding.bindings) == {"url"}
    assert len(binding.rows) == 9


async def test_the_dropped_columns_leave_the_declared_contract_too() -> None:
    """Dropping a column from the bindings alone would trade an unbound field
    for an unsaveable document: `validate_document` refuses a table column with
    no candidates bound, which is the error the real build died with."""

    binding, _reason = await _verify_images(_Reader(json=IMAGE_ROWS), _image_cols())

    assert binding is not None and binding.narrowed_spec is not None
    assert set(binding.narrowed_spec.type.columns) == {"url"}


async def test_a_table_no_column_fills_is_still_rejected() -> None:
    """Salvaging is for a table that partly worked. One where nothing resolved
    is a wrong answer, and saying so is what gets a better one proposed."""

    nothing = [{"url": None, "alt_text": None, "position": None} for _ in range(9)]
    binding, reason = await _verify_images(_Reader(json=nothing), _image_cols())

    assert binding is None
    assert reason and "empty" in reason


async def test_a_partly_filled_column_is_still_a_bad_selector() -> None:
    """Empty in EVERY row means the page has no such datum. Empty in most rows
    means it was resolved against the wrong thing -- still worth rejecting, so
    the model is asked for a better one."""

    patchy = [
        {"url": f"https://x.test/{i}.jpg", "alt_text": "shot" if i < 2 else None, "position": 1}
        for i in range(9)
    ]
    binding, reason = await _verify_images(_Reader(json=patchy), _image_cols())

    assert binding is None
    assert reason and "alt_text" in reason


async def test_an_open_map_keeps_its_own_columns() -> None:
    """`MAP_COLUMNS` are this module's name/value pair, not something the caller
    declared -- narrowing them would describe a map that is not a map."""

    spec = FieldSpec(name="origin", type=TypeSpec(kind="object"))
    rows = [{"name": "Made in", "value": None}, {"name": "Imported by", "value": None}]
    binding, reason = await verify_rows(
        spec, "dom_rows", Locator(kind="css", selector="li"),
        _cols(name=".k", value=".v"),
        reader=_Reader(rows=rows),  # type: ignore[arg-type]
        page_url="https://x.test/p",
    )

    # `value` is empty everywhere, so this is not salvageable as a map.
    assert binding is None
    assert reason and "value" in reason
