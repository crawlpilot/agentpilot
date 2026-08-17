"""Pure unit tests for `agentpilot.recipe.evaluate`'s `json_ld`/`hydration`/
`meta`/`ax_role` branches -- all four are testable without a live driver
when `structured_data`/`snapshot` are passed in directly (the `css` branch
always needs a live dispatch, so it's covered by a driver_contract test
instead). `session`/`registry`/`driver` are never touched on these branches,
so plain `None` placeholders are fine here."""

from __future__ import annotations

from agentpilot.recipe.evaluate import evaluate_field_locator, find_ax_role_refs
from agentpilot.recipe.models import FieldLocator
from tests.fusion_fixtures import fnode

STRUCTURED_DATA = {
    "json_ld": [{"@type": "Product", "offers": {"price": "19.99"}}],
    "hydration": {"__NEXT_DATA__": {"props": {"pageProps": {"title": "Widget"}}}},
    "metadata": {"og:title": "Widget Page"},
}


async def test_json_ld_field_locator_resolves_via_dotted_path() -> None:
    locator = FieldLocator(source="json_ld", path="[0].offers.price")
    value = await evaluate_field_locator(
        locator, structured_data=STRUCTURED_DATA, session=None, registry=None, driver=None
    )
    assert value == "19.99"


async def test_hydration_field_locator_resolves_via_nested_path() -> None:
    locator = FieldLocator(source="hydration", path="__NEXT_DATA__.props.pageProps.title")
    value = await evaluate_field_locator(
        locator, structured_data=STRUCTURED_DATA, session=None, registry=None, driver=None
    )
    assert value == "Widget"


async def test_meta_field_locator_resolves_via_key() -> None:
    locator = FieldLocator(source="meta", path="og:title")
    value = await evaluate_field_locator(
        locator, structured_data=STRUCTURED_DATA, session=None, registry=None, driver=None
    )
    assert value == "Widget Page"


async def test_json_ld_field_locator_missing_path_resolves_to_none() -> None:
    locator = FieldLocator(source="json_ld", path="[0].nonexistent")
    value = await evaluate_field_locator(
        locator, structured_data=STRUCTURED_DATA, session=None, registry=None, driver=None
    )
    assert value is None


async def test_ax_role_field_locator_resolves_to_the_matched_nodes_name() -> None:
    snapshot = fnode("root", children=[fnode("button", "Add to cart", ref="e1")])
    locator = FieldLocator(source="ax_role", role="button", name_contains="Add")
    value = await evaluate_field_locator(
        locator, structured_data=None, session=None, registry=None, driver=None, snapshot=snapshot
    )
    assert value == "Add to cart"


async def test_ax_role_field_locator_no_match_resolves_to_none() -> None:
    snapshot = fnode("root", children=[fnode("button", "Add to cart", ref="e1")])
    locator = FieldLocator(source="ax_role", role="button", name_contains="Checkout")
    value = await evaluate_field_locator(
        locator, structured_data=None, session=None, registry=None, driver=None, snapshot=snapshot
    )
    assert value is None


def test_find_ax_role_refs_matches_by_role_and_name_in_set() -> None:
    snapshot = fnode(
        "root",
        children=[
            fnode("button", "S", ref="e1"),
            fnode("button", "M", ref="e2"),
            fnode("link", "M", ref="e3"),  # different role -- must not match
        ],
    )
    refs = find_ax_role_refs(snapshot, "button", name_in=["S", "M"])
    assert refs == ["e1", "e2"]
