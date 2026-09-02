"""The v2 transform pipeline.

The first block is every behavioural case from the v1 suite
(`test_recipe_normalize.py`), restated as transform lists. That is the
correctness bar for the rewrite: v2 is allowed to change the shape of the
contract, not to quietly lose a decade of accumulated handling for real pages.
"""

from __future__ import annotations

import pytest

from agentpilot.recipe.v2.transform import (
    Transform,
    TransformContext,
    TransformError,
    apply_transforms,
    parse_transforms,
)


def run(value, ops, **ctx_kw):
    return apply_transforms(value, parse_transforms(ops), TransformContext(**ctx_kw))


# --- parity with v1 normalize.py -------------------------------------------


def test_price_extracts_first_float_stripping_currency_and_separators() -> None:
    assert run("$1,299.00", [{"op": "cast", "to": "price"}]) == 1299.0


def test_number_handles_leading_label_and_percent() -> None:
    assert run("Rating: 4.5 out of 5", [{"op": "cast", "to": "number"}]) == 4.5


def test_integer_truncates_float_text() -> None:
    assert run("1,024 reviews", [{"op": "cast", "to": "integer"}]) == 1024


def test_boolean_maps_known_tokens() -> None:
    assert run("In Stock", [{"op": "cast", "to": "boolean"}]) is True
    assert run("out of stock", [{"op": "cast", "to": "boolean"}]) is False


def test_unrecognized_boolean_falls_back_to_default() -> None:
    ops = [{"op": "cast", "to": "boolean"}, {"op": "default", "value": False}]
    assert run("maybe", ops) is False


def test_url_absolutizes_against_base() -> None:
    got = run("/p/42", [{"op": "cast", "to": "url"}], url="https://x.test/cat/")
    assert got == "https://x.test/p/42"


def test_date_parses_common_layout_to_iso() -> None:
    assert run("Jan 5, 2026", [{"op": "cast", "to": "date"}]) == "2026-01-05"


def test_datetime_parses_iso_with_zulu() -> None:
    got = run("2026-01-05T10:30:00Z", [{"op": "cast", "to": "datetime"}])
    assert got.startswith("2026-01-05T10:30:00")


def test_regex_extract_group_then_coerce() -> None:
    ops = [
        {"op": "regex_extract", "pattern": r"(\d+)\s*g", "group": 1},
        {"op": "cast", "to": "integer"},
    ]
    assert run("Net weight 500 g", ops) == 500


def test_replace_rules_apply_in_order() -> None:
    ops = [
        {"op": "regex_replace", "pattern": " ", "repl": " "},
        {"op": "regex_replace", "pattern": "SALE ", "repl": ""},
    ]
    assert run("SALE Widget Pro", ops) == "Widget Pro"


def test_collapse_ws_and_trim_and_case_and_accents() -> None:
    ops = [
        {"op": "collapse_ws"},
        {"op": "case", "mode": "lower"},
        {"op": "strip_accents"},
    ]
    assert run("  Café   Crème  ", ops) == "cafe creme"


def test_control_chars_are_stripped() -> None:
    assert run("a\x00b\x1fc", [{"op": "strip_control"}]) == "abc"


def test_none_uses_default() -> None:
    assert run(None, [{"op": "default", "value": "n/a"}]) == "n/a"


def test_regex_no_match_falls_back_to_default_not_crash() -> None:
    ops = [
        {"op": "regex_extract", "pattern": r"(\d+)", "group": 1},
        {"op": "default", "value": "none"},
    ]
    assert run("no digits here", ops) == "none"


def test_invalid_regex_is_a_transform_error_not_a_crash() -> None:
    """v1 swallowed a bad pattern silently. v2 surfaces it: an unbalanced group
    in a stored recipe is an authoring bug, and a field that quietly returns
    the default forever is how that bug survives to production."""

    with pytest.raises(TransformError, match="invalid regex"):
        run("anything", [{"op": "regex_extract", "pattern": "(unclosed"}])


# --- ordering is now explicit ----------------------------------------------


def test_order_is_caller_controlled_unlike_v1() -> None:
    """v1's fixed order could not express "cast, then default"."""
    ops = [{"op": "cast", "to": "integer"}, {"op": "default", "value": -1}]
    assert run("not a number", ops) == -1


# --- new ops ----------------------------------------------------------------


def test_split_then_scalar_op_maps_over_the_list() -> None:
    ops = [{"op": "split", "sep": ","}, {"op": "trim"}]
    assert run("a , b ,c", ops) == ["a", "b", "c"]


def test_join_is_a_list_op() -> None:
    assert run(["a", "b"], [{"op": "join", "sep": "-"}]) == "a-b"


def test_index_and_slice_and_unique_and_filter_empty() -> None:
    assert run(["a", "b", "c"], [{"op": "index", "i": 1}]) == "b"
    assert run(["a", "a", "b"], [{"op": "unique"}]) == ["a", "b"]
    assert run(["a", "", None, "b"], [{"op": "filter_empty"}]) == ["a", "b"]
    assert run("abcdef", [{"op": "slice", "start": 1, "end": 3}]) == "bc"


def test_map_lookup_with_explicit_null_default_keeps_unknowns_honest() -> None:
    """Observed vocabularies: Zara ships schema.org URLs, Walmart ships
    OUT_OF_STOCK. An unseen token must not silently become False."""

    ops = [
        {"op": "regex_extract", "pattern": "([^/]+)$", "group": 1},
        {
            "op": "map_lookup",
            "table": {"InStock": True, "OutOfStock": False},
            "default": None,
        },
    ]
    assert run("https://schema.org/OutOfStock", ops) is False
    assert run("https://schema.org/BackOrder", ops) is None


def test_template_interpolates_value_and_metadata() -> None:
    ops = [{"op": "template", "format": "{{v}} ({{meta.region}})"}]
    assert run("Dress", ops, meta={"region": "in"}) == "Dress (in)"


def test_template_missing_metadata_key_is_empty_not_an_error() -> None:
    assert run("x", [{"op": "template", "format": "{{v}}{{meta.nope}}"}]) == "x"


def test_url_resolve_uses_the_run_url() -> None:
    got = run("img.jpg", [{"op": "url_resolve"}], url="https://x.test/a/b.html")
    assert got == "https://x.test/a/img.jpg"


def test_to_object_turns_name_value_rows_into_the_requested_map() -> None:
    """The near-universal spec shape. Walmart's idml.specifications and
    Amazon's th/td rows both arrive like this."""

    rows = [
        {"name": "Scent", "value": "Strawberry Cookie"},
        {"name": "Form", "value": "Liquid"},
    ]
    got = run(rows, [{"op": "to_object", "key": "name", "value": "value"}])
    assert got == {"Scent": "Strawberry Cookie", "Form": "Liquid"}


def test_to_object_skips_rows_missing_either_side() -> None:
    rows = [{"name": "Scent", "value": "x"}, {"name": "  ", "value": "y"}, {"name": "Form"}]
    assert run(rows, [{"op": "to_object", "key": "name", "value": "value"}]) == {"Scent": "x"}


def test_to_pairs_is_the_inverse() -> None:
    got = run({"a": 1}, [{"op": "to_pairs"}])
    assert got == [{"key": "a", "value": 1}]


def test_strip_html_flattens_a_markup_string() -> None:
    got = run("<ul> <li>One</li> <li>Two &amp; more</li> </ul>", [{"op": "strip_html"}])
    assert got == "One Two & more"


def test_html_select_pulls_bullets_out_of_a_json_string() -> None:
    """Walmart's idml.longDescription is a JSON *string* containing markup, and
    the caller wants the bullets as a list."""

    html = "<ul>  <li>Crumbl for Strawberry Crumb Cake</li>  <li>Notes of rich strawberry</li></ul>"
    ops = [{"op": "html_select", "selector": "li", "attribute": "text", "all": True}]
    assert run(html, ops) == ["Crumbl for Strawberry Crumb Cake", "Notes of rich strawberry"]


def test_html_select_can_read_an_attribute() -> None:
    ops = [{"op": "html_select", "selector": "a", "attribute": "href", "all": True}]
    assert run('<p><a href="/x">x</a><a href="/y">y</a></p>', ops) == ["/x", "/y"]


def test_json_parse_then_json_path() -> None:
    ops = [{"op": "json_parse"}, {"op": "json_path", "path": "a.b"}]
    assert run('{"a": {"b": 7}}', ops) == 7


def test_json_path_accepts_jmespath_with_a_filter() -> None:
    data = {"specs": [{"name": "Scent", "value": "Cookie"}, {"name": "Form", "value": "Liquid"}]}
    ops = [{
        "op": "json_path",
        "path_lang": "jmespath",
        "path": "specs[?name=='Scent'].value | [0]",
    }]
    assert run(data, ops) == "Cookie"


def test_invisible_marks_are_stripped() -> None:
    """Amazon pads its feature bullets with zero-width/bidi marks, which
    survive every whitespace-based cleanup because they are not whitespace."""

    got = run("‎ Wheels roll for push around play.‏", [
        {"op": "strip_control"}, {"op": "collapse_ws"},
    ])
    assert got == "Wheels roll for push around play."


def test_empty_list_and_dict_count_as_empty_for_default() -> None:
    assert run([], [{"op": "default", "value": "fallback"}]) == "fallback"
    assert run({}, [{"op": "default", "value": "fallback"}]) == "fallback"


def test_scalar_ops_drop_none_results_when_mapping_a_list() -> None:
    ops = [{"op": "regex_extract", "pattern": r"(\d+)", "group": 1}]
    assert run(["a1", "nope", "b2"], ops) == ["1", "2"]


def test_transform_round_trips_through_dict() -> None:
    t = Transform(op="regex_extract", pattern=r"(\d+)", group=1, flags="i")
    assert Transform.from_dict(t.to_dict()) == t


def test_to_dict_omits_defaults() -> None:
    assert Transform(op="trim").to_dict() == {"op": "trim"}


def test_unknown_op_is_an_error() -> None:
    with pytest.raises(TransformError, match="unknown transform op"):
        apply_transforms("x", [Transform(op="nope")])  # type: ignore[arg-type]
