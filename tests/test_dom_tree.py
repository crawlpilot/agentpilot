"""Unit tests for `agentpilot.spi.dom_tree.EnhancedDOMTreeNode` -- structural
identity, xpath, and shadow/child traversal built on synthetic nodes. No CDP."""

from __future__ import annotations

from agentpilot.spi.dom_tree import EnhancedAXNode, EnhancedDOMTreeNode, NodeType


def _el(
    name: str,
    *,
    backend: int,
    attrs: dict[str, str] | None = None,
    ax_name: str | None = None,
    node_type: NodeType = NodeType.ELEMENT_NODE,
    value: str = "",
) -> EnhancedDOMTreeNode:
    return EnhancedDOMTreeNode(
        node_id=backend,
        backend_node_id=backend,
        node_type=node_type,
        node_name=name,
        node_value=value,
        attributes=attrs or {},
        ax_node=EnhancedAXNode(role=None, name=ax_name) if ax_name is not None else None,
    )


def _link(parent: EnhancedDOMTreeNode, child: EnhancedDOMTreeNode) -> EnhancedDOMTreeNode:
    child.parent_node = parent
    parent.children_nodes.append(child)
    return child


def _tree() -> tuple[EnhancedDOMTreeNode, EnhancedDOMTreeNode]:
    html = _el("HTML", backend=1)
    body = _link(html, _el("BODY", backend=2))
    div = _link(body, _el("DIV", backend=3))
    button = _link(
        div, _el("BUTTON", backend=4, attrs={"id": "buy", "class": "btn"}, ax_name="Buy")
    )
    return html, button


def test_parent_branch_path_element_only() -> None:
    _, button = _tree()
    assert button.parent_branch_path() == ["html", "body", "div", "button"]


def test_stable_hash_matches_pure_hashing() -> None:
    from agentpilot.spi import hashing

    _, button = _tree()
    assert button.stable_hash() == hashing.stable_hash(
        ["html", "body", "div", "button"], {"id": "buy", "class": "btn"}, "Buy"
    )


def test_stable_hash_survives_dynamic_class_but_exact_does_not() -> None:
    _, button = _tree()
    before_stable, before_exact = button.stable_hash(), button.element_hash()
    button.attributes["class"] = "btn is-hover open"
    assert button.stable_hash() == before_stable
    assert button.element_hash() != before_exact


def test_xpath_indexes_same_tag_siblings_and_stops_at_iframe() -> None:
    html, _ = _tree()
    div = html.children_nodes[0].children_nodes[0]
    # Two <p> siblings -> 1-based predicates; a lone <span> -> no predicate.
    p1 = _link(div, _el("P", backend=10))
    _link(div, _el("P", backend=11))
    span = _link(div, _el("SPAN", backend=12))
    assert p1.xpath().endswith("p[1]")
    assert span.xpath().endswith("span")
    assert span.xpath().startswith("html/body/div")

    # An iframe boundary makes the xpath frame-local: nothing at or above the
    # iframe appears (matching browser-use's break-before-append semantics).
    iframe = _link(div, _el("IFRAME", backend=20))
    inner = _link(iframe, _el("A", backend=21))
    child = _link(inner, _el("SPAN", backend=22))
    xp = child.xpath()
    assert "iframe" not in xp and "div" not in xp
    assert xp == "span"


def test_children_and_shadow_roots_merge_without_mutation() -> None:
    host = _el("DIV", backend=30)
    _link(host, _el("SPAN", backend=31))
    shadow = _el("SHADOW", backend=32, node_type=NodeType.DOCUMENT_FRAGMENT_NODE)
    host.shadow_roots.append(shadow)

    merged = host.children_and_shadow_roots
    assert [n.backend_node_id for n in merged] == [31, 32]
    # The convenience view must not have mutated the real children list.
    assert [n.backend_node_id for n in host.children_nodes] == [31]


def test_all_text_collects_descendant_text_nodes() -> None:
    div = _el("DIV", backend=40)
    _link(div, _el("#text", backend=41, node_type=NodeType.TEXT_NODE, value="Hello "))
    span = _link(div, _el("SPAN", backend=42))
    _link(span, _el("#text", backend=43, node_type=NodeType.TEXT_NODE, value="world"))
    assert div.all_text() == "Hello \nworld"


def test_identity_equality_not_structural() -> None:
    a = _el("DIV", backend=1)
    b = _el("DIV", backend=1)
    assert a == a
    assert a != b  # eq=False -> identity, even with identical fields
