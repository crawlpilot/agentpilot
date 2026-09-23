"""What is in the blob, and where -- checked against a real page's shape.

The fixture is `tests/fixtures/walmart_401967617_structured.json`: the
structured data of the Walmart product page from the bug report, with the key
names, path shapes and the ad trap taken from the measured worked example at
`docs/examples/recipes/walmart-product.v2.json`. Its `contentLayout` and
`telemetry` padding stands in for the volume that makes the live blob 352 KB --
what matters is that `idml` sits far past any prefix cut, because "the answer
was off the end of the prompt" is the failure this module exists to fix.

These are the first tests in the suite to run against a real page's JSON rather
than a hand-built stub, which is the point: every claim below is a claim about
a page that exists.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agentpilot.recipe.v2.contract import fields_from_output_schema
from agentpilot.recipe.v2.json_index import (
    confident_value_arrays,
    find_row_arrays,
    find_value_arrays,
    is_noise_key,
    name_affinity,
    outline,
)
from agentpilot.recipe.v2.rows import wanted_columns
from agentpilot.recipe.v2.schema import FieldSpec, TypeSpec

FIXTURE = Path(__file__).parent / "fixtures" / "walmart_401967617_structured.json"

# The schema from the bug report, verbatim.
OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "highlights": {
            "type": "array",
            "description": (
                "Key product benefits, features, selling points, or bullet highlights"
            ),
            "items": {"type": "string"},
        },
        "specifications": {
            "type": "array",
            "description": "Structured product specifications and attribute-value pairs",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "value": {"type": "string"},
                },
            },
        },
    },
    "required": ["highlights", "specifications"],
}

IDML = "__NEXT_DATA__.props.pageProps.initialData.data.idml"


@pytest.fixture
def structured() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text())


@pytest.fixture
def fields() -> dict[str, FieldSpec]:
    return fields_from_output_schema(OUTPUT_SCHEMA)


def test_the_bug_reports_schema_maps_to_a_list_and_a_table(fields) -> None:
    """The two halves of the request take different paths through the build, and
    that is why one of them worked and the other did not."""

    assert fields["highlights"].type.kind == "list"
    assert fields["specifications"].type.kind == "table"
    assert set(fields["specifications"].type.columns) == {"name", "value"}


# ---------------------------------------------------------------------------
# The outline
# ---------------------------------------------------------------------------


def test_the_answer_is_visible_where_a_raw_prefix_would_have_cut_it(
    structured, fields
) -> None:
    """The whole bug in one assertion.

    `json.dumps(blob)[:12_000]` did not reach `idml` on this page, so the model
    was asked to write an anchored path into data it had never seen.
    """

    raw_prefix = json.dumps(structured, ensure_ascii=False)[:12_000]
    assert "productHighlights" not in raw_prefix
    assert f"{IDML}.specifications" not in raw_prefix

    text = outline(structured, wanted=fields, max_chars=16_000)
    assert f"{IDML}.specifications" in text
    assert f"{IDML}.productHighlights" in text


def test_the_wanted_arrays_come_first(structured, fields) -> None:
    """Under a budget the order is the whole design: the caller's own words for
    what they want are the only signal about which paths matter."""

    lines = outline(structured, wanted=fields, max_chars=16_000).splitlines()
    heads = [ln for ln in lines if ln.startswith("kind=")]
    assert f"path='{IDML}.specifications'" in heads[0]
    assert f"path='{IDML}.productHighlights'" in heads[1]


def test_the_ad_trap_is_not_in_the_outline_at_all(structured) -> None:
    """`paths.py` documents it: this page carries a sponsored competitor under
    `contentLayout.modules[].configs.ad` with the same key names as the real
    product. Not showing it beats warning about it."""

    text = outline(structured, max_chars=40_000)
    assert "St. Ives" not in text
    assert "adContentV2" not in text
    # And the real product is still there -- the pruning is targeted, not a
    # blanket refusal to describe `contentLayout`.
    assert "Bodycology" in text


def test_a_top_level_scalar_is_addressable(structured) -> None:
    """A leaf gets its own path rather than being bundled into its parent's
    line. That is how a `title` or a `price` binds, and a leaf mentioned only
    inside an object summary would not tell the model how to address it."""

    text = outline(structured, max_chars=40_000)
    assert "kind=meta path='og:title'" in text
    assert (
        "kind=hydration path="
        "'__NEXT_DATA__.props.pageProps.initialData.data.product.name'" in text
    )


def test_a_lone_oversized_value_is_still_reported_as_truncated() -> None:
    """Silently cutting would have the model conclude a key is absent when it
    was merely shown in part."""

    text = outline({"hydration": {"k": "x" * 40_000}}, max_chars=2_000)
    assert "path='k'" in text
    assert "TRUNCATED" in text


def test_the_outline_respects_its_budget(structured, fields) -> None:
    text = outline(structured, wanted=fields, max_chars=1_500)
    assert len(text) <= 1_500 + 120  # + the trailing truncation note
    assert "TRUNCATED" in text
    # Even squeezed this hard, the field the caller asked about survives.
    assert f"{IDML}.specifications" in text


def test_an_empty_blob_outlines_to_nothing() -> None:
    assert outline({}) == ""
    assert outline({"hydration": {}, "json_ld": [], "metadata": {}}) == ""


# ---------------------------------------------------------------------------
# Rows -- the behaviour moved out of rows.py, which must not regress
# ---------------------------------------------------------------------------


def test_specifications_outranks_the_other_name_value_arrays(structured, fields) -> None:
    """`idml.specifications`, `idml.productHighlights` and `idml.indications` are
    all `[{name, value}]` and all verify. Only one is the specification sheet,
    and `name_affinity` is the entire reason the right one wins."""

    found = find_row_arrays(
        structured, wanted_columns(fields["specifications"]), "specifications"
    )
    assert found[0][0] == "hydration"
    assert found[0][1] == f"{IDML}.specifications"
    assert found[0][2][0] == {"name": "Brand", "value": "Bodycology"}


def test_row_discovery_never_offers_the_sponsored_competitor(structured, fields) -> None:
    found = find_row_arrays(
        structured, wanted_columns(fields["specifications"]), "specifications"
    )
    assert all("configs.ad" not in path for _kind, path, _sample in found)
    assert all(
        "St. Ives" not in json.dumps(sample) for _kind, _path, sample in found
    )


# ---------------------------------------------------------------------------
# Value arrays -- the new path, and the reason `highlights` could not bind
# ---------------------------------------------------------------------------


def test_highlights_is_found_as_a_projection(structured, fields) -> None:
    """A list of strings whose values are one key of an array of objects needs
    `productHighlights[*].value`, and dotted traversal cannot say that."""

    found = find_value_arrays(structured, fields["highlights"], "highlights")
    paths = [c.path for c in found]
    assert f"{IDML}.productHighlights[*].value" in paths
    assert all(c.path_lang == "jmespath" for c in found if "[*]" in c.path)


def test_a_name_value_array_is_offered_both_ways_and_binds_neither(
    structured, fields
) -> None:
    """For a field called `highlights`, "Skin type" and "All" are each half of
    one fact and nothing in the data says which half was asked for. Binding the
    winner of that tie is a coin flip that looks like a measurement -- so both
    readings go to the model, which has the field's description to judge with.
    """

    found = find_value_arrays(structured, fields["highlights"], "highlights")
    top = [c for c in found if c.root == f"{IDML}.productHighlights"]
    assert {c.path.rsplit(".", 1)[-1] for c in top} == {"name", "value"}
    assert top[0].score == top[1].score

    assert confident_value_arrays(structured, fields["highlights"], "highlights") == []


def test_an_unambiguous_array_does_bind_without_a_model(structured) -> None:
    """The gate is ambiguity, not caution: a single-key array whose values match
    the declared type is an answer, not a guess."""

    images = FieldSpec(
        name="images",
        type=TypeSpec(kind="list", items=TypeSpec(kind="scalar", value_type="url")),
    )
    confident = confident_value_arrays(structured, images, "images")
    assert [c.path for c in confident] == [
        "__NEXT_DATA__.props.pageProps.initialData.data.product.imageInfo.allImages[*].url"
    ]
    assert confident[0].sample[0].startswith("https://")


def test_an_array_the_site_names_differently_is_never_auto_bound(structured) -> None:
    """Affinity is what stops a well-shaped array under an unrelated key from
    binding silently -- `verify_locators` cannot help here, because "reads a
    non-empty array" is true of a great many arrays on this page."""

    unrelated = FieldSpec(
        name="warranty_terms",
        type=TypeSpec(kind="list", items=TypeSpec(kind="scalar", value_type="string")),
    )
    assert confident_value_arrays(structured, unrelated, "warranty_terms") == []
    # It is still offered as a hint, where the page snapshot is available to
    # judge it against.
    assert find_value_arrays(structured, unrelated, "warranty_terms")


def test_a_table_field_yields_no_value_arrays(structured, fields) -> None:
    """`specifications` is a table and goes through `rows.py`. Offering it here
    too would bind it twice, in two shapes."""

    assert find_value_arrays(structured, fields["specifications"], "specifications") == []


# ---------------------------------------------------------------------------
# The pieces
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "field", "expected"),
    [
        ("a.b.specifications", "specifications", 60),
        ("a.b.productHighlights", "highlights", 40),
        ("a.b.indications", "highlights", 0),
        ("a.highlights.rows", "highlights", 20),
        ("a.b.c", "", 0),
    ],
)
def test_name_affinity(path: str, field: str, expected: int) -> None:
    assert name_affinity(path, field) == expected


@pytest.mark.parametrize(
    "key", ["ad", "ads", "adsEnabled", "adContentV2", "telemetry", "__typename",
            "sponsoredProducts", "analytics", "beacon"],
)
def test_noise_keys_are_recognised(key: str) -> None:
    assert is_noise_key(key)


@pytest.mark.parametrize(
    "key", ["shadeName", "gradeLabel", "address", "name", "value", "product",
            "loadedAt", "headline", "brand"],
)
def test_real_data_keys_are_not_mistaken_for_noise(key: str) -> None:
    """A bare "ad" substring match would drop `shadeName` and `gradeLabel`,
    which is a worse bug than showing an advertisement."""

    assert not is_noise_key(key)
