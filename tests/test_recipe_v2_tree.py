"""Fused-tree matching for `ax_role` and `text` locators.

Two v1 limitations are fixed here and both are asserted rather than assumed:
`ax_role` can read attributes (v1 could only ever return the accessible name),
and `within` actually scopes (v1's `name_in` matched the whole tree, which its
own docstring flagged as an accepted false-positive risk).
"""

from __future__ import annotations

from fusion_fixtures import fnode

from agentpilot.recipe.v2.models import Locator
from agentpilot.recipe.v2.tree import find_nodes, node_attribute, node_text
from crawlpilot.spi.dom_tree import EnhancedDOMTreeNode, NodeType


def with_attrs(node: EnhancedDOMTreeNode, **attrs: str) -> EnhancedDOMTreeNode:
    node.attributes = dict(attrs)
    return node


def text_node(value: str) -> EnhancedDOMTreeNode:
    return EnhancedDOMTreeNode(
        node_id=0, backend_node_id=0, node_type=NodeType.TEXT_NODE,
        node_name="#text", node_value=value, is_visible=True,
    )


# --- role matching ----------------------------------------------------------


def test_matches_by_role_and_name_substring() -> None:
    tree = fnode(children=[
        fnode("button", "PRODUCT MEASUREMENTS", "e1"),
        fnode("button", "ADD TO CART", "e2"),
    ])
    got = find_nodes(tree, Locator(kind="ax_role", role="button", name_contains="MEASUREMENT"))
    assert [n.ax_name for n in got] == ["PRODUCT MEASUREMENTS"]


def test_name_in_enumerates_an_option_set() -> None:
    tree = fnode(children=[fnode("button", s, f"e{i}") for i, s in enumerate("XSML")])
    got = find_nodes(tree, Locator(kind="ax_role", role="button", name_in=["X", "M"]))
    assert [n.ax_name for n in got] == ["X", "M"]


def test_name_regex_matches() -> None:
    tree = fnode(children=[fnode("text", "62 cm", "e1"), fnode("text", "Length", "e2")])
    got = find_nodes(tree, Locator(kind="ax_role", role="text", name_regex=r"^\d+\s*cm$"))
    assert [n.ax_name for n in got] == ["62 cm"]


def test_invalid_name_regex_matches_nothing_rather_than_raising() -> None:
    tree = fnode(children=[fnode("text", "x", "e1")])
    assert find_nodes(tree, Locator(kind="ax_role", role="text", name_regex="(unclosed")) == []


def test_role_alone_matches_every_node_of_that_role() -> None:
    tree = fnode(children=[fnode("button", "a", "e1"), fnode("link", "b", "e2"),
                           fnode("button", "c", "e3")])
    got = find_nodes(tree, Locator(kind="ax_role", role="button"))
    assert [n.ax_name for n in got] == ["a", "c"]


def test_matches_are_in_document_order() -> None:
    tree = fnode(children=[
        fnode("button", "first", "e1"),
        fnode(children=[fnode("button", "second", "e2")]),
        fnode("button", "third", "e3"),
    ])
    got = find_nodes(tree, Locator(kind="ax_role", role="button"))
    assert [n.ax_name for n in got] == ["first", "second", "third"]


# --- within scoping: the v1 false positive ---------------------------------


def test_within_confines_an_enumerated_name_set() -> None:
    """The real shape: a size-guide drawer renders XS/S/M while the page behind
    it renders the same labels. Without scoping, an option set matched
    whole-tree picks up the wrong element."""

    tree = fnode(children=[
        fnode("group", "page size selector", "e1", children=[
            fnode("button", "S", "e2"),
        ]),
        fnode("group", "size guide drawer", "e3", children=[
            fnode("button", "S", "e4"),
        ]),
    ])

    unscoped = find_nodes(tree, Locator(kind="ax_role", role="button", name_in=["S"]))
    assert len(unscoped) == 2, "whole-tree matching is the v1 behaviour"

    scoped = find_nodes(tree, Locator(
        kind="ax_role", role="button", name_in=["S"],
        within=Locator(kind="ax_role", role="group", name_contains="drawer"),
    ))
    assert [n.backend_node_id for n in scoped] == [4]


def test_within_that_matches_nothing_yields_no_matches() -> None:
    tree = fnode(children=[fnode("button", "S", "e1")])
    got = find_nodes(tree, Locator(
        kind="ax_role", role="button",
        within=Locator(kind="ax_role", role="group", name_contains="absent"),
    ))
    assert got == []


def test_a_css_within_is_refused_rather_than_silently_widened() -> None:
    """Silently ignoring a scope the tree cannot honour is how the v1
    false-positive happened."""

    tree = fnode(children=[fnode("button", "S", "e1")])
    got = find_nodes(tree, Locator(
        kind="ax_role", role="button", within=Locator(kind="css", selector=".drawer"),
    ))
    assert got == []


# --- attributes: the other v1 limitation ------------------------------------


def test_ax_role_can_now_read_an_href() -> None:
    """v1 returned `matches[0].ax_name` unconditionally, so this was
    impossible."""

    link = with_attrs(fnode("link", "Next", "e1"), href="/page/2", **{"data-qa": "next"})
    tree = fnode(children=[link])
    node = find_nodes(tree, Locator(kind="ax_role", role="link"))[0]
    assert node_attribute(node, "href") == "/page/2"
    assert node_attribute(node, "data-qa") == "next"


def test_text_attribute_still_means_the_accessible_name() -> None:
    node = fnode("button", "ADD", "e1")
    assert node_attribute(node, "text") == "ADD"


def test_missing_attribute_is_none() -> None:
    assert node_attribute(fnode("link", "x", "e1"), "href") is None


def test_text_falls_back_to_descendant_text_when_there_is_no_accessible_name() -> None:
    container = fnode("generic", "", "e1", children=[text_node("62 cm")])
    assert node_text(container) == "62 cm"


# --- text locators ----------------------------------------------------------


def test_text_locator_matches_on_a_substring_case_insensitively() -> None:
    tree = fnode(children=[
        fnode("button", "PRODUCT MEASUREMENTS", "e1"),
        fnode("button", "ADD", "e2"),
    ])
    got = find_nodes(tree, Locator(kind="text", text="measurements"))
    assert [n.backend_node_id for n in got] == [1]


def test_css_and_xpath_locators_are_not_tree_resolvable() -> None:
    """They go through the page's own engines in evaluate.py."""
    tree = fnode(children=[fnode("button", "x", "e1")])
    assert find_nodes(tree, Locator(kind="css", selector="button")) == []
    assert find_nodes(tree, Locator(kind="xpath", selector="//button")) == []
