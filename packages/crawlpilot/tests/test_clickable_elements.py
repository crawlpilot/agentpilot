"""Unit tests for `crawlpilot.dom.clickable_elements.is_interactive` -- one case
per branch of the ported interactivity ladder. Pure node inputs, no browser."""

from __future__ import annotations

from crawlpilot.dom.clickable_elements import is_interactive
from crawlpilot.driver.dom_fusion import LayoutInfo
from crawlpilot.spi.dom_tree import EnhancedAXNode, EnhancedDOMTreeNode, NodeType
from crawlpilot.spi.geometry import BoundingBox


def _node(
    name: str = "div",
    *,
    attrs: dict[str, str] | None = None,
    ax_role: str | None = None,
    ax_props: dict[str, str | bool] | None = None,
    node_type: NodeType = NodeType.ELEMENT_NODE,
    js_listener: bool = False,
    bounds: tuple[float, float] | None = None,
    cursor: str | None = None,
    is_clickable: bool = False,
) -> EnhancedDOMTreeNode:
    snapshot = None
    if bounds is not None or cursor is not None or is_clickable:
        w, h = bounds or (0.0, 0.0)
        snapshot = LayoutInfo(
            bounds=BoundingBox(0, 0, w, h) if bounds is not None else None,
            cursor_style=cursor,
            is_clickable=is_clickable,
        )
    return EnhancedDOMTreeNode(
        node_id=1,
        backend_node_id=1,
        node_type=node_type,
        node_name=name,
        attributes=attrs or {},
        ax_node=(
            EnhancedAXNode(role=ax_role, properties=ax_props or {})
            if ax_role is not None or ax_props is not None
            else None
        ),
        has_js_click_listener=js_listener,
        snapshot=snapshot,
    )


def test_non_element_and_structural_tags_rejected() -> None:
    assert not is_interactive(_node("#text", node_type=NodeType.TEXT_NODE))
    assert not is_interactive(_node("html"))
    assert not is_interactive(_node("body"))
    assert not is_interactive(_node("div"))  # plain div, no signals


def test_js_click_listener_wins() -> None:
    assert is_interactive(_node("div", js_listener=True))


def test_large_iframe_interactive_small_not() -> None:
    assert is_interactive(_node("iframe", bounds=(300, 300)))
    assert not is_interactive(_node("iframe", bounds=(50, 50)))


def test_label_for_rejected_wrapper_accepted() -> None:
    assert not is_interactive(_node("label", attrs={"for": "email"}))
    label = _node("label")
    label.children_nodes.append(_node("input"))
    label.children_nodes[0].parent_node = label
    assert is_interactive(label)


def test_search_indicator_class_and_data_attr() -> None:
    assert is_interactive(_node("div", attrs={"class": "header search-icon"}))
    assert is_interactive(_node("div", attrs={"id": "site-search"}))
    assert is_interactive(_node("div", attrs={"data-role": "magnify"}))


def test_ax_disabled_hard_rejects_even_native_tag() -> None:
    # aria disabled must veto before the native-tag check grants it.
    assert not is_interactive(_node("button", ax_props={"disabled": True}))


def test_ax_state_presence_and_truthy_props() -> None:
    assert is_interactive(_node("div", ax_props={"checked": False}))  # presence
    assert is_interactive(_node("div", ax_props={"focusable": True}))  # truthy
    assert not is_interactive(_node("div", ax_props={"focusable": False}))


def test_native_tags_and_roles() -> None:
    assert is_interactive(_node("button"))
    assert is_interactive(_node("a"))
    assert is_interactive(_node("div", attrs={"role": "combobox"}))
    assert is_interactive(_node("div", ax_role="slider"))
    assert is_interactive(_node("div", attrs={"onclick": "f()"}))
    assert is_interactive(_node("div", attrs={"tabindex": "0"}))


def test_icon_sized_with_affordance() -> None:
    assert is_interactive(_node("i", attrs={"aria-label": "menu"}, bounds=(24, 24)))
    # Same size, no affordance attribute -> not interactive on size alone.
    assert not is_interactive(_node("i", bounds=(24, 24)))


def test_pointer_cursor_and_chrome_clickable_fallbacks() -> None:
    assert is_interactive(_node("div", cursor="pointer"))
    assert is_interactive(_node("div", is_clickable=True))
    assert not is_interactive(_node("div", cursor="default"))


def test_tabindex_is_read_by_value_not_by_presence() -> None:
    """`tabindex="-1"` means *removed from tab order* -- the opposite of the
    signal a presence check reads it as. Authors use it to make a scroll
    container or a modal wrapper programmatically focusable, and every one of
    those was being offered to the model as a control."""

    assert is_interactive(_node("div", attrs={"tabindex": "0"}))
    assert is_interactive(_node("div", attrs={"tabindex": "3"}))
    assert not is_interactive(_node("div", attrs={"tabindex": "-1"}))
    # Whitespace is legal around the value; garbage is treated as absent.
    assert is_interactive(_node("div", attrs={"tabindex": " 0 "}))
    assert not is_interactive(_node("div", attrs={"tabindex": "yes"}))


def test_contenteditable_is_a_text_input_by_another_name() -> None:
    # Bare `contenteditable` parses as the empty string, which enables editing.
    assert is_interactive(_node("div", attrs={"contenteditable": ""}))
    assert is_interactive(_node("div", attrs={"contenteditable": "true"}))
    assert is_interactive(_node("div", attrs={"contenteditable": "TRUE"}))
    assert not is_interactive(_node("div", attrs={"contenteditable": "false"}))
    # `inherit` defers to an ancestor, which is classified on its own merits.
    assert not is_interactive(_node("div", attrs={"contenteditable": "inherit"}))


def _with_parent(child: EnhancedDOMTreeNode, parent: EnhancedDOMTreeNode) -> EnhancedDOMTreeNode:
    parent.children_nodes.append(child)
    child.parent_node = parent
    return child


def test_inherited_pointer_cursor_does_not_promote_a_whole_subtree() -> None:
    """`cursor` inherits, so one `cursor: pointer` on a card container gives the
    same computed style to every div, span and image inside it. The container
    keeps its ref; the descendants that have nothing else going for them do
    not."""

    card = _node("div", cursor="pointer")
    inner = _with_parent(_node("span", cursor="pointer"), card)

    assert is_interactive(card)
    assert not is_interactive(inner)


def test_a_child_with_its_own_affordance_keeps_its_ref_inside_a_pointer_parent() -> None:
    """The inherited-cursor rule is the *weakest* rung of the ladder, so it can
    only ever drop a node that reached it -- one with no other signal. A real
    control inside a clickable card returned True long before."""

    card = _node("div", cursor="pointer")
    assert is_interactive(_with_parent(_node("button", cursor="pointer"), card))
    assert is_interactive(_with_parent(_node("div", attrs={"onclick": "f()"}, cursor="pointer"), card))
    assert is_interactive(_with_parent(_node("div", attrs={"role": "menuitem"}, cursor="pointer"), card))


def test_pointer_cursor_still_counts_when_the_parent_has_none() -> None:
    plain = _node("div", cursor="default")
    assert is_interactive(_with_parent(_node("span", cursor="pointer"), plain))
    # A parent with no layout info at all must not be read as "pointer".
    assert is_interactive(_with_parent(_node("span", cursor="pointer"), _node("div")))
