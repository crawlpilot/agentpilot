"""Path resolution into the structured-data containers, in both dialects."""

from __future__ import annotations

import pytest

from agentpilot.recipe.v2.paths import PathError, resolve_path

# A miniature of the real Walmart __NEXT_DATA__ shape, including the thing that
# makes recursive descent dangerous: the page's own JSON carries a SPONSORED
# COMPETITOR's product under contentLayout, with the same key names as the
# product being scraped.
WALMART_ISH = {
    "props": {"pageProps": {"initialData": {"data": {
        "product": {
            "name": "Dove Body Wash Strawberry Cookie 20 fl oz",
            "priceInfo": {"currentPrice": {"price": 6.97, "currencyUnit": "USD"}},
            "availabilityStatus": "OUT_OF_STOCK",
        },
        "idml": {
            "specifications": [
                {"name": "Primary ingredient", "value": "Strawberry Crumb Cake"},
                {"name": "Scent", "value": "Strawberry Cookie"},
                {"name": "Form", "value": "Liquid"},
            ],
            "directions": [{"name": "Instructions", "value": "Squeeze onto a wet pouf."}],
        },
        "contentLayout": {"modules": [{"configs": {"ad": {"adContentV2": {"data": {
            "products": [{"name": "St. Ives Soothing Body Wash", "brand": "St. Ives"}]
        }}}}}]},
    }}}}
}


# --- simple dialect ---------------------------------------------------------


def test_simple_dotted_and_bracket_traversal() -> None:
    assert resolve_path({"a": {"b": [1, 2]}}, "a.b[1]") == 2
    assert resolve_path({"a": {"b": [1, 2]}}, "a.b.0") == 1


def test_simple_returns_none_for_a_missing_key_or_index() -> None:
    assert resolve_path({"a": 1}, "b.c") is None
    assert resolve_path({"a": [1]}, "a[9]") is None


def test_simple_returns_none_when_traversing_a_scalar() -> None:
    assert resolve_path({"a": 1}, "a.b") is None


def test_empty_path_is_the_whole_document() -> None:
    assert resolve_path({"a": 1}, "") == {"a": 1}


def test_simple_reaches_the_real_walmart_shape() -> None:
    got = resolve_path(
        WALMART_ISH, "props.pageProps.initialData.data.product.priceInfo.currentPrice.price"
    )
    assert got == 6.97


# --- jmespath dialect -------------------------------------------------------


def test_jmespath_filters_an_unordered_array_by_a_sibling_key() -> None:
    """The thing the simple dialect cannot do, and the reason the second
    dialect exists: spec rows are unordered, so positional indexing is not
    stable across products."""

    got = resolve_path(
        WALMART_ISH,
        "props.pageProps.initialData.data.idml.specifications[?name=='Scent'].value | [0]",
        "jmespath",
    )
    assert got == "Strawberry Cookie"


def test_jmespath_projects_rows_into_the_pairs_to_object_expects() -> None:
    got = resolve_path(
        WALMART_ISH,
        "props.pageProps.initialData.data.idml.specifications[].{name: name, value: value}",
        "jmespath",
    )
    assert got[1] == {"name": "Scent", "value": "Strawberry Cookie"}


def test_jmespath_no_match_is_none_not_an_error() -> None:
    got = resolve_path(
        WALMART_ISH,
        "props.pageProps.initialData.data.idml.specifications[?name=='Nope'].value | [0]",
        "jmespath",
    )
    assert got is None


def test_malformed_jmespath_raises_rather_than_silently_returning_none() -> None:
    """An authoring bug worth surfacing. A page that merely lacks the value is
    a different thing, and returns None."""

    with pytest.raises(PathError, match="invalid jmespath"):
        resolve_path(WALMART_ISH, "specs[?name==", "jmespath")


# --- the reason neither dialect offers recursive descent ---------------------


def test_neither_dialect_can_express_unanchored_descent() -> None:
    """Measured on the real page: `$..name` matches 203 nodes and returns a
    competitor's advertisement among its first distinct results. Both dialects
    are anchored-only by design, so the shortest expression that "works" cannot
    be the one that silently scrapes the wrong brand.

    The ad IS reachable -- by naming its path in full, which is exactly the
    property we want: reaching it has to be deliberate.
    """

    assert resolve_path(WALMART_ISH, "..name") is None
    assert resolve_path(WALMART_ISH, "name", "jmespath") is None

    anchored = resolve_path(
        WALMART_ISH,
        "props.pageProps.initialData.data.product.name",
    )
    ad = resolve_path(
        WALMART_ISH,
        "props.pageProps.initialData.data.contentLayout.modules[0]"
        ".configs.ad.adContentV2.data.products[0].name",
    )
    assert anchored.startswith("Dove")
    assert ad.startswith("St. Ives")
