"""The ref lifecycle: mint, resolve, invalidate.

A `ref` is an `e<n>` string a snapshot hands out. It resolves to a captured
fused node by dictionary lookup and nothing else -- no CSS, no XPath, no
Playwright selector engine -- which is what lets it reach inside iframes and
shadow roots, and what makes it *capture-scoped*: the index is dropped on every
snapshot and every navigation, so a ref from a replaced capture must never
resolve against the new DOM. Silently resolving one is how you click a lookalike.

This is the mechanism behind `STALE_REF`, and nothing exercised it in CI. The
tests that did live in `tests/driver_contract/` (which both CI jobs pass
`--ignore` to) and `tests/browser/` (deselected by `-m 'not browser'`), so the
whole ref flow was covered only by suites that never run. These are unit tests
over `NodeIndex` and need no browser.
"""

from __future__ import annotations

from crawlpilot.driver.node_index import NodeIndex
from crawlpilot.spi.dom_tree import (
    EnhancedAXNode,
    EnhancedDOMTreeNode,
    NodeType,
    assign_selector_indices,
)


def _element(
    backend_id: int,
    *,
    tag: str = "div",
    role: str = "",
    name: str = "",
    children: list[EnhancedDOMTreeNode] | None = None,
    content_document: EnhancedDOMTreeNode | None = None,
) -> EnhancedDOMTreeNode:
    node = EnhancedDOMTreeNode(
        node_id=backend_id,
        backend_node_id=backend_id,
        node_type=NodeType.ELEMENT_NODE,
        node_name=tag.upper(),
        is_visible=True,
        ax_node=EnhancedAXNode(role=role or None, name=name or None),
        children_nodes=list(children or []),
        content_document=content_document,
    )
    node.selector_index = backend_id
    for child in node.children_nodes:
        child.parent_node = node
    return node


def _page(*ids: int) -> EnhancedDOMTreeNode:
    return _element(1, tag="body", children=[_element(i, tag="button") for i in ids])


# ------------------------------------------------------------------ minting


def test_a_capture_mints_a_ref_for_every_element() -> None:
    index = NodeIndex()
    index.record(_page(12, 13))

    assert "e12" in index
    assert "e13" in index
    assert index.get("e12") is not None
    assert index.get("e12").backend_node_id == 12  # type: ignore[union-attr]


def test_refs_reach_inside_an_iframe() -> None:
    """The reason refs exist rather than selectors. `document.querySelector`
    searches one document; a ref resolves through the fused tree, so an element
    in an iframe's content document is addressable exactly like any other."""

    inner = _element(99, tag="input")
    frame = _element(50, tag="iframe", content_document=_element(60, children=[inner]))
    index = NodeIndex()
    index.record(_element(1, tag="body", children=[frame]))

    assert "e99" in index
    assert index.get("e99") is not None


def test_every_element_is_addressable_not_only_the_rendered_ones() -> None:
    """The serializer decides what is worth *showing* a model. A caller that
    already holds a ref -- a replayed recipe, an extension -- should not be
    limited by that editorial choice."""

    index = NodeIndex()
    index.record(_element(1, tag="body", children=[_element(7, tag="span")]))

    assert "e7" in index


def test_a_selector_match_rejoins_the_same_node() -> None:
    """A CSS selector resolves *in Chrome* to a backend node id; that id then has
    to find its way to the same fused node every verb downstream expects. This
    is the parallel index that makes the two paths meet."""

    index = NodeIndex()
    index.record(_page(12))

    assert index.by_backend_id(12) is index.get("e12")


# ------------------------------------------------------------ invalidation


def test_a_navigation_drops_every_ref() -> None:
    """`reset()` is what a navigation calls. Without it a ref taken before the
    navigation could resolve against an unrelated element on the new page --
    the same backend id, a completely different button."""

    index = NodeIndex()
    index.record(_page(12))
    assert "e12" in index

    index.reset()

    assert "e12" not in index
    assert index.get("e12") is None
    assert len(index) == 0


def test_a_fresh_capture_replaces_the_previous_one() -> None:
    index = NodeIndex()
    index.record(_page(12, 13))
    index.record(_page(14))

    assert "e14" in index
    assert "e12" not in index
    assert "e13" not in index


# ---------------------------------------------------------------- the miss
#
# Every way a ref fails to resolve is one dict miss, and the answer is always
# "snapshot again". browser-use's model exactly: `get_dom_element_by_index`
# returns the node or `None`, and the caller says "page may have changed. Try
# refreshing browser state."


def test_a_ref_from_a_superseded_capture_does_not_resolve() -> None:
    """The point of dropping the index: if this resolved, a click would land on
    whatever element happens to hold that id on the *new* page."""

    index = NodeIndex()
    index.record(_page(12))
    index.record(_page(14))

    assert index.get("e12") is None


def test_a_ref_that_never_existed_does_not_resolve() -> None:
    index = NodeIndex()
    index.record(_page(12))

    assert index.get("e999") is None
    # A selector passed where a ref belongs -- the query verbs take a ref
    # positionally, so `is_visible("#buy")` arrives here as a ref.
    assert index.get("#buy") is None


def test_the_message_tells_the_caller_what_to_do() -> None:
    """One actionable sentence rather than a cause the caller cannot act on
    differently. Both misses above produce this."""

    from crawlpilot.spi.errors import StaleRefError

    error = StaleRefError("e12")

    assert error.ref == "e12"
    assert "e12" in str(error)
    assert "fresh snapshot" in str(error)


# ----------------------------------------------------- collision resolution


def test_two_renderers_reusing_a_backend_id_get_distinct_refs() -> None:
    """Cross-origin iframes number their nodes independently, so the same
    `backendNodeId` can appear twice in one fused tree. A ref must still
    identify one element -- `assign_selector_indices` gives the duplicate a
    synthetic index above every real id."""

    duplicate = _element(7, tag="input")
    frame = _element(50, tag="iframe", content_document=_element(60, children=[duplicate]))
    root = _element(1, tag="body", children=[_element(7, tag="button"), frame])

    assign_selector_indices(root)
    index = NodeIndex()
    index.record(root)

    refs = {f"e{n.selector_index}" for n in (duplicate, root.children_nodes[0])}
    assert len(refs) == 2, "a duplicate backend id collapsed two elements onto one ref"
    assert all(ref in index for ref in refs)
