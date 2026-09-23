"""Unit tests for the serializer pipeline (`crawlpilot.dom.paint_order` +
`crawlpilot.dom.serializer` + `crawlpilot.dom.render`): occlusion, containment
dedup, backend-id indexing, attribute compression, password redaction, and
truncation. Synthetic fused trees; no browser."""

from __future__ import annotations

from crawlpilot.dom.paint_order import PaintEntry, Rect, RectUnionPure, compute_occluded
from crawlpilot.dom.render import normalize_text
from crawlpilot.dom.serializer import serialize
from crawlpilot.driver.dom_fusion import LayoutInfo
from crawlpilot.spi.dom_tree import EnhancedAXNode, EnhancedDOMTreeNode, NodeType, SnapshotView
from crawlpilot.spi.geometry import BoundingBox

# --------------------------------------------------------------------- paint order


def test_rect_union_covers_when_tiled() -> None:
    u = RectUnionPure()
    u.add(Rect(0, 0, 10, 10))
    u.add(Rect(10, 0, 20, 10))
    assert u.contains(Rect(2, 2, 18, 8))  # spanning both tiles
    assert not u.contains(Rect(2, 2, 22, 8))  # pokes past the union


def test_compute_occluded_front_covers_back() -> None:
    back = PaintEntry(key=1, x=0, y=0, width=100, height=100, paint_order=1)
    front = PaintEntry(key=2, x=0, y=0, width=100, height=100, paint_order=5)
    assert compute_occluded([back, front]) == {1}


def test_translucent_front_does_not_occlude() -> None:
    back = PaintEntry(key=1, x=0, y=0, width=100, height=100, paint_order=1)
    front = PaintEntry(key=2, x=0, y=0, width=100, height=100, paint_order=5, opacity=0.3)
    assert compute_occluded([back, front]) == set()


# --------------------------------------------------------------------- fixtures


def _node(
    tag: str,
    backend: int,
    *,
    attrs: dict[str, str] | None = None,
    ax_role: str | None = None,
    ax_name: str = "",
    node_type: NodeType = NodeType.ELEMENT_NODE,
    value: str = "",
    bounds: BoundingBox | None = None,
    paint_order: int | None = None,
    visible: bool = True,
    bg: str | None = None,
    cursor: str | None = None,
    styles: dict[str, str] | None = None,
) -> EnhancedDOMTreeNode:
    snapshot = None
    if bounds is not None or paint_order is not None or cursor is not None or styles:
        styles = {"display": "block", **(styles or {})}
        if bg is not None:
            styles["background-color"] = bg
        snapshot = LayoutInfo(
            bounds=bounds,
            paint_order=paint_order,
            cursor_style=cursor,
            computed_styles=styles,
        )
    return EnhancedDOMTreeNode(
        node_id=backend,
        backend_node_id=backend,
        node_type=node_type,
        node_name=tag.upper() if node_type == NodeType.ELEMENT_NODE else tag,
        node_value=value,
        attributes=attrs or {},
        is_visible=visible,
        absolute_position=bounds,
        snapshot=snapshot,
        ax_node=EnhancedAXNode(role=ax_role, name=ax_name) if ax_role or ax_name else None,
    )


def _child(parent: EnhancedDOMTreeNode, node: EnhancedDOMTreeNode) -> EnhancedDOMTreeNode:
    node.parent_node = parent
    parent.children_nodes.append(node)
    return node


# --------------------------------------------------------------------- serializer


def test_disabled_tags_and_svg_collapsed() -> None:
    body = _node("body", 1)
    _child(body, _node("script", 2, value="x"))
    _child(body, _node("style", 3))
    svg = _child(body, _node("svg", 4, ax_role="img"))
    _child(svg, _node("path", 5))  # svg internals must not appear
    btn = _child(body, _node("button", 6, ax_name="Go", bounds=BoundingBox(0, 0, 40, 20)))
    _ = btn

    result = serialize(body)
    assert "script" not in result.llm_text and "style" not in result.llm_text
    assert "path" not in result.llm_text
    assert "[e6]" in result.llm_text  # the button survived


def test_index_is_backend_node_id_and_new_marker() -> None:
    body = _node("body", 1)
    _child(body, _node("button", 42, ax_name="Buy", bounds=BoundingBox(0, 0, 50, 20)))
    result = serialize(body, new_backend_ids={42})
    assert set(result.selector_map) == {42}
    assert result.selector_map[42].ax_name == "Buy"
    assert "*[e42]" in result.llm_text  # NEW marker


def test_containment_dedup_button_in_button() -> None:
    # Outer clickable div fully containing an inner clickable span with no
    # keep-exception attrs (interactive only via ax role) -> inner is deduped.
    outer = _node("div", 1, attrs={"onclick": "f()"}, bounds=BoundingBox(0, 0, 200, 50))
    inner = _child(outer, _node("span", 2, ax_role="button", bounds=BoundingBox(5, 5, 50, 20)))
    _ = inner
    result = serialize(outer)
    assert 1 in result.selector_map
    assert 2 not in result.selector_map  # contained -> excluded


def test_paint_order_occlusion_removes_covered_interactive() -> None:
    body = _node("body", 1)
    hidden = _child(
        body,
        _node("button", 2, ax_name="Behind", bounds=BoundingBox(0, 0, 100, 100), paint_order=1),
    )
    overlay = _child(
        body,
        _node(
            "button",
            3,
            ax_name="Overlay",
            bounds=BoundingBox(0, 0, 100, 100),
            paint_order=9,
            bg="rgb(255,255,255)",  # opaque -> actually hides what's behind
        ),
    )
    _ = (hidden, overlay)
    result = serialize(body)
    assert 3 in result.selector_map
    assert 2 not in result.selector_map  # occluded


def test_attribute_whitelist_and_password_redaction() -> None:
    body = _node("body", 1)
    _child(
        body,
        _node(
            "input",
            2,
            attrs={"type": "password", "value": "hunter2", "data-secret": "leak"},
            bounds=BoundingBox(0, 0, 100, 20),
        ),
    )
    text = serialize(body).llm_text
    assert "hunter2" not in text  # redacted
    assert "<redacted>" in text
    assert "data-secret" not in text  # not in the whitelist


def test_truncation_marker_added_when_over_budget() -> None:
    body = _node("body", 1)
    for i in range(50):
        _child(body, _node("button", 100 + i, ax_name=f"Item {i}", bounds=BoundingBox(0, i, 80, 1)))
    text = serialize(body, max_length=200).llm_text
    assert len(text) <= 200
    assert "truncated" in text


def test_compression_reduces_node_count() -> None:
    # 3 real interactive buttons buried under wrapper/structural divs + noise.
    body = _node("body", 1)
    for i in range(3):
        wrapper = _child(body, _node("div", 10 + i))  # structural, no text
        _child(wrapper, _node("button", 20 + i, ax_name=f"B{i}", bounds=BoundingBox(0, i, 40, 10)))
    _child(body, _node("script", 99, value="noise"))
    result = serialize(body)
    # Only the 3 buttons get indexed; wrappers/script contribute no refs.
    assert set(result.selector_map) == {20, 21, 22}
    assert result.llm_text.count("[e") == 3


# --------------------------------------------------------- invisible characters


def test_normalize_text_deletes_zero_width_and_folds_exotic_spaces() -> None:
    assert normalize_text("​Buy﻿ now⁠") == "Buy now"
    # A string of nothing but zero-width characters is empty to a reader.
    assert normalize_text("​﻿⁠‌‍") == ""
    assert normalize_text("  spaced   out  ") == "spaced out"


def test_normalize_text_keeps_nbsp_separated_words_apart() -> None:
    """The divergence from agent-browser's `INVISIBLE_CHARS`, which deletes
    `U+00A0` along with the zero-width set and so renders this `Addtocart` --
    a name the model cannot match against anything it reads on the page."""

    assert normalize_text("Add to cart") == "Add to cart"
    assert normalize_text("a　b c") == "a b c"


def test_a_text_node_of_only_zero_width_characters_is_dropped() -> None:
    body = _node("body", 1)
    _child(body, _node("#text", 2, node_type=NodeType.TEXT_NODE, value="​﻿"))
    _child(body, _node("#text", 3, node_type=NodeType.TEXT_NODE, value="real text"))

    text = serialize(body).llm_text
    assert "real text" in text
    # One line, not two: the invisible node contributed nothing and so must not
    # have cost an indentation level either.
    assert text.count("\n") == 0


def test_zero_width_padding_does_not_change_the_rendered_name() -> None:
    """Two captures of the same button, one padded with a zero-width space by an
    anti-scraping script, must render identically -- otherwise every step reports
    the element as changed."""

    def render_button(name: str) -> str:
        body = _node("body", 1)
        _child(body, _node("button", 7, ax_name=name, bounds=BoundingBox(0, 0, 50, 20)))
        return serialize(body).llm_text

    assert render_button("​Checkout​") == render_button("Checkout")


def test_an_attribute_that_normalizes_to_the_accessible_name_is_still_deduped() -> None:
    body = _node("body", 1)
    _child(
        body,
        _node(
            "button",
            8,
            attrs={"aria-label": "Submit​"},
            ax_name="Submit",
            bounds=BoundingBox(0, 0, 50, 20),
        ),
    )
    text = serialize(body).llm_text
    assert '"Submit"' in text
    assert "aria-label" not in text


# ------------------------------------------------- hidden-control promotion


def _card_wrapping_hidden_radio(
    *, hide: dict[str, str] | None = None, attrs: dict[str, str] | None = None
) -> EnhancedDOMTreeNode:
    """The component-library shape: a `<label>` drawn as a card, wrapping an
    input Chrome has dropped from the accessibility tree."""

    body = _node("body", 1)
    label = _child(body, _node("label", 10, bounds=BoundingBox(0, 0, 200, 80)))
    _child(label, _node("input", 11, attrs=attrs or {"type": "radio"}, styles=hide))
    return body


def test_a_label_wrapping_a_display_none_radio_renders_as_a_radio() -> None:
    text = serialize(_card_wrapping_hidden_radio(hide={"display": "none"})).llm_text
    assert "[e10]<radio" in text
    assert "checked=false" in text


def test_promotion_reads_the_checked_state_off_the_control() -> None:
    body = _card_wrapping_hidden_radio(
        hide={"display": "none"}, attrs={"type": "radio", "checked": ""}
    )
    assert "checked=true" in serialize(body).llm_text


def test_promotion_accepts_visibility_hidden_and_the_hidden_attribute() -> None:
    assert "[e10]<radio" in serialize(
        _card_wrapping_hidden_radio(hide={"visibility": "hidden"})
    ).llm_text
    assert "[e10]<checkbox" in serialize(
        _card_wrapping_hidden_radio(attrs={"type": "checkbox", "hidden": ""})
    ).llm_text


def test_a_visible_control_is_not_promoted_onto_its_wrapper() -> None:
    """A visible input keeps its own accessibility node, so promoting the label
    too would show the model the same control twice."""

    text = serialize(_card_wrapping_hidden_radio()).llm_text
    assert "[e10]<radio" not in text


def test_only_radios_and_checkboxes_are_promoted() -> None:
    body = _card_wrapping_hidden_radio(
        hide={"display": "none"}, attrs={"type": "hidden", "name": "csrf"}
    )
    assert "<radio" not in serialize(body).llm_text


# ------------------------------------------------------------- SnapshotView


def _page_of_buttons(count: int = 4, *, y_step: float = 100.0) -> EnhancedDOMTreeNode:
    """`count` buttons stacked down the page, ids 10, 11, 12..."""

    body = _node("body", 1)
    for i in range(count):
        _child(
            body,
            _node(
                "button",
                10 + i,
                ax_name=f"Button {i}",
                bounds=BoundingBox(0, i * y_step, 80, 30),
            ),
        )
    return body


def _offered(body: EnhancedDOMTreeNode, view: SnapshotView | None = None) -> set[int]:
    return set(serialize(body, view=view).selector_map)


def test_no_view_offers_everything_addressable() -> None:
    body = _page_of_buttons()
    assert _offered(body) == {10, 11, 12, 13}
    assert _offered(body, SnapshotView()) == {10, 11, 12, 13}


def test_max_nodes_keeps_the_elements_nearest_the_top_of_the_page() -> None:
    """In document order, not an arbitrary subset -- the cap should keep what a
    reader reaches first."""

    assert _offered(_page_of_buttons(), SnapshotView(max_nodes=2)) == {10, 11}


def test_roles_filter_offers_only_the_named_roles() -> None:
    body = _node("body", 1)
    _child(
        body,
        _node("button", 20, ax_role="button", ax_name="Go", bounds=BoundingBox(0, 0, 40, 20)),
    )
    _child(body, _node("a", 21, ax_role="link", ax_name="Home", bounds=BoundingBox(0, 30, 40, 20)))

    assert _offered(body, SnapshotView(roles=("link",))) == {21}
    assert _offered(body, SnapshotView(roles=("button", "link"))) == {20, 21}


def test_viewport_only_drops_what_is_below_the_fold() -> None:
    body = _page_of_buttons(count=4, y_step=500)
    viewport = BoundingBox(0, 0, 1280, 720)

    # Buttons at y=0 and y=500 intersect a 720-tall viewport; y=1000 and 1500 do not.
    assert _offered(body, SnapshotView(viewport=viewport)) == {10, 11}


def test_viewport_only_keeps_an_element_that_has_no_geometry_at_all() -> None:
    """A hidden file input or a checkbox behind a styled label reports no box.
    That is not "outside the viewport" -- dropping it would silently remove
    controls that work perfectly well."""

    body = _node("body", 1)
    _child(body, _node("input", 30, attrs={"type": "file"}))  # no bounds
    _child(body, _node("button", 31, ax_name="Go", bounds=BoundingBox(0, 0, 40, 20)))

    assert _offered(body, SnapshotView(viewport=BoundingBox(0, 0, 1280, 720))) == {30, 31}


def test_scope_offers_only_the_selectors_subtree() -> None:
    body = _page_of_buttons()
    assert _offered(body, SnapshotView(scope=frozenset({11, 13}))) == {11, 13}


def test_filters_compose() -> None:
    body = _page_of_buttons(count=4, y_step=10)
    view = SnapshotView(scope=frozenset({10, 11, 12}), max_nodes=2)
    assert _offered(body, view) == {10, 11}


def test_a_hidden_element_is_still_addressable_it_is_only_not_offered() -> None:
    """The view narrows what the model is *shown*. Nothing is pruned from the
    tree, so occlusion still works and a caller naming the ref still resolves
    it -- which is why the flag is `excluded_by_view`, not a deletion."""

    body = _page_of_buttons()
    text = serialize(body, view=SnapshotView(max_nodes=1)).llm_text

    assert "[e10]" in text
    assert "[e11]" not in text, "not offered"
    # But the full tree is untouched: serializing again without a view sees it.
    assert 11 in serialize(body).selector_map


def test_an_occluder_the_view_hides_still_occludes() -> None:
    """The reason the filters run *after* paint-order: a modal scrim dropped by a
    role filter must not stop hiding the buttons behind it."""

    body = _node("body", 1)
    _child(
        body,
        _node("button", 40, ax_role="button", ax_name="Behind",
              bounds=BoundingBox(0, 0, 100, 100), paint_order=1),
    )
    _child(
        body,
        _node("div", 41, ax_role="generic",
              bounds=BoundingBox(0, 0, 200, 200), paint_order=9, bg="rgb(0, 0, 0)"),
    )

    # The scrim's role is filtered out, yet the button it covers stays hidden.
    assert _offered(body, SnapshotView(roles=("button",))) == set()


# ---------------------------------------------------------------- depth


def test_depth_caps_how_deep_a_rendered_line_may_sit() -> None:
    body = _node("body", 1)
    outer = _child(body, _node("button", 50, ax_name="Outer", bounds=BoundingBox(0, 0, 200, 200)))
    inner = _child(
        outer, _node("span", 51, attrs={"onclick": "f()"}, bounds=BoundingBox(0, 0, 10, 10))
    )
    _child(inner, _node("#text", 52, node_type=NodeType.TEXT_NODE, value="deep text"))

    full = serialize(body).llm_text
    assert "deep text" in full

    shallow = serialize(body, view=SnapshotView(depth=0)).llm_text
    assert "[e50]" in shallow, "the top level still renders"
    assert "deep text" not in shallow


def test_depth_counts_rendered_lines_not_dom_nesting() -> None:
    """Ten structural wrappers above a button do not make it deep: they emit no
    lines, so it is still the reader's first level."""

    body = _node("body", 1)
    parent = body
    for i in range(10):
        parent = _child(parent, _node("div", 60 + i))
    _child(parent, _node("button", 90, ax_name="Buried", bounds=BoundingBox(0, 0, 40, 20)))

    assert "[e90]" in serialize(body, view=SnapshotView(depth=0)).llm_text


# ------------------------------------------------------- visible_text_only
#
# The size of an agent observation was decided by a head-first character cap,
# and on a commerce product page that cap ran out inside the nav. The controls
# the task needed -- four accordion buttons, last in document order -- were
# never rendered, so `rendered_indices` excluded them, so the loop REJECTED
# every ref the model chose for them. Its own reasoning ("scroll them into view
# to get refs") could not help: nothing here filters by scroll position, so the
# same prefix came back every step until the budget ran out.
#
# The fix is to stop rendering what the agent cannot act on in the first place,
# which is browser-use's rule: a text node is rendered only if it is visible,
# not painted over, and longer than one character.


def test_hidden_text_is_still_rendered_by_default() -> None:
    """The recipe builder depends on this and must not be disturbed: a `text`
    locator is specified to read textContent INCLUDING collapsed content, so a
    field behind a shut accordion is findable without clicking it, and the judge
    corroborates values against this same render."""

    body = _node("body", 1)
    panel = _child(body, _node("div", 2, visible=False))
    _child(panel, _node("#text", 3, node_type=NodeType.TEXT_NODE, value="100% viscose", visible=False))

    assert "100% viscose" in serialize(body).llm_text


def test_visible_text_only_drops_collapsed_content() -> None:
    body = _node("body", 1)
    panel = _child(body, _node("div", 2, visible=False))
    _child(panel, _node("#text", 3, node_type=NodeType.TEXT_NODE, value="100% viscose", visible=False))
    _child(body, _node("#text", 4, node_type=NodeType.TEXT_NODE, value="Add to cart"))

    text = serialize(body, view=SnapshotView(visible_text_only=True)).llm_text
    assert "100% viscose" not in text
    assert "Add to cart" in text


def test_visible_text_only_keeps_text_whose_visibility_is_unknown() -> None:
    """`is_visible` is tri-state and `None` means the fusion could not say. The
    honest reading of "unknown" is to keep the text -- dropping it would make
    the flag lose real content on any page the layout snapshot did not cover."""

    body = _node("body", 1)
    unknown = _node("#text", 2, node_type=NodeType.TEXT_NODE, value="probably readable")
    unknown.is_visible = None
    _child(body, unknown)

    assert "probably readable" in serialize(
        body, view=SnapshotView(visible_text_only=True)
    ).llm_text


def test_visible_text_only_drops_single_character_text() -> None:
    """Separators and stray glyphs, one render line each."""

    body = _node("body", 1)
    _child(body, _node("#text", 2, node_type=NodeType.TEXT_NODE, value="·"))
    _child(body, _node("#text", 3, node_type=NodeType.TEXT_NODE, value="Checkout"))

    text = serialize(body, view=SnapshotView(visible_text_only=True)).llm_text
    assert "·" not in text
    assert "Checkout" in text


def test_visible_text_only_never_costs_a_ref() -> None:
    """Text filtering must not change which elements are addressable -- only how
    much text surrounds them."""

    body = _node("body", 1)
    hidden = _child(body, _node("div", 2, visible=False))
    _child(hidden, _node("#text", 3, node_type=NodeType.TEXT_NODE, value="hidden blurb", visible=False))
    _child(body, _node("button", 4, ax_role="button", ax_name="Composition, care & origin"))

    plain = serialize(body)
    filtered = serialize(body, view=SnapshotView(visible_text_only=True))
    assert set(filtered.selector_map) == set(plain.selector_map)
    assert "Composition, care & origin" in filtered.llm_text


def test_the_controls_survive_a_budget_that_the_page_text_would_have_eaten() -> None:
    """The Zara failure, in miniature. Bulk hidden text first, the control the
    task needs last, and a budget smaller than the two together.

    Without the filter the cap is spent before the button renders, so it earns
    no place in `rendered_indices` -- and the agent loop validates the model's
    chosen ref against exactly that set, so the button becomes unclickable
    rather than merely unmentioned.
    """

    body = _node("body", 1)
    for i in range(60):
        blurb = _child(body, _node("div", 100 + i, visible=False))
        _child(
            blurb,
            _node(
                "#text", 1000 + i, node_type=NodeType.TEXT_NODE,
                value=f"Shipping and returns boilerplate paragraph {i}. " * 6,
                visible=False,
            ),
        )
    _child(body, _node("button", 9, ax_role="button", ax_name="Composition, care & origin"))

    budget = 4_000
    plain = serialize(body, max_length=budget)
    filtered = serialize(body, max_length=budget, view=SnapshotView(visible_text_only=True))

    assert 9 not in plain.rendered_indices, "the control is lost to the cap today"
    assert "truncated" in plain.llm_text
    assert 9 in filtered.rendered_indices
    assert "truncated" not in filtered.llm_text


def test_the_truncation_marker_only_claims_what_truncation_does() -> None:
    """Two different things keep an element out of a render and they need
    opposite remedies, so the marker must not speak for both.

    This cut is by document order against a character budget -- scrolling does
    not move it. But `is_visible` is separately viewport-gated, and scrolling
    genuinely does fix that; the offscreen-controls trailer reports it. A marker
    that promises scrolling works, or that it does not, is wrong half the time.
    """

    body = _node("body", 1)
    for i in range(50):
        _child(body, _node("button", 100 + i, ax_name=f"Item {i}", bounds=BoundingBox(0, i, 80, 1)))

    text = serialize(body, max_length=600).llm_text
    assert "truncated" in text
    assert "scroll" not in text.lower()


def test_the_marker_gives_way_rather_than_bust_a_small_budget() -> None:
    body = _node("body", 1)
    for i in range(50):
        _child(body, _node("button", 100 + i, ax_name=f"Item {i}", bounds=BoundingBox(0, i, 80, 1)))

    for budget in (10, 40, 120, 600):
        text = serialize(body, max_length=budget).llm_text
        # `max_length` means what it says, even below the marker's own length.
        assert len(text) <= budget, f"budget {budget} overrun"
    # Once there is room to say it, it is said.
    assert "truncated" in serialize(body, max_length=120).llm_text


def test_a_button_inside_a_clickable_wrapper_keeps_its_own_ref() -> None:
    """A `<button>` is a real control and speaks for itself, exactly as the
    form-control tags do -- it was simply missing from that set.

    Sites wrap an accordion trigger in a clickable container; the button then
    fills ~100% of its wrapper and the containment dedup drops it. Unlike the
    viewport gate, NOTHING brings it back -- scrolling does not help -- which is
    what made it so hard to see from outside. A Zara build spent its last four
    steps on "the accordion button isn't minting a ref", scrolled to it exactly
    as instructed, and still got nothing.
    """

    wrapper = _node("div", 1, attrs={"onclick": "toggle()"}, bounds=BoundingBox(0, 0, 300, 60))
    _child(
        wrapper,
        _node(
            "button", 2, ax_role="button", ax_name="Composition, care & origin",
            bounds=BoundingBox(0, 0, 300, 60),
        ),
    )

    result = serialize(wrapper)
    assert 2 in result.selector_map, "the button must be nameable"
    assert "Composition, care & origin" in result.llm_text


def test_a_link_inside_a_clickable_wrapper_keeps_its_own_ref() -> None:
    wrapper = _node("div", 1, attrs={"onclick": "go()"}, bounds=BoundingBox(0, 0, 200, 40))
    _child(wrapper, _node("a", 2, ax_role="link", ax_name="Size guide", bounds=BoundingBox(0, 0, 200, 40)))

    assert 2 in serialize(wrapper).selector_map


def test_a_plain_span_inside_a_clickable_wrapper_is_still_deduped() -> None:
    """The dedup still earns its keep: a bare interactive-by-role `<span>`
    filling its clickable parent is one control reported twice."""

    wrapper = _node("div", 1, attrs={"onclick": "go()"}, bounds=BoundingBox(0, 0, 200, 40))
    _child(wrapper, _node("span", 2, ax_role="button", bounds=BoundingBox(0, 0, 200, 40)))

    result = serialize(wrapper)
    assert 1 in result.selector_map
    assert 2 not in result.selector_map


def test_a_nameless_image_control_is_described_by_its_image() -> None:
    """An image button's only description lives on the `<img>` inside it, which
    is not interactive and so renders no line of its own. Without this the
    control reaches the model as an anonymous `<button />`.

    Measured on a Zara product page: eight gallery controls named "Side view of
    a multicoloured bag with an asymmetric top" in the accessibility tree, every
    one of them rendering as `[eN]<button />`.
    """

    button = _node("button", 2, bounds=BoundingBox(0, 0, 80, 80))
    _child(button, _node("img", 3, attrs={"alt": "Side view of a multicoloured bag"}))
    body = _node("body", 1)
    _child(body, button)

    text = serialize(body).llm_text
    assert "image_alt=Side view of a multicoloured bag" in text


def test_a_control_that_already_says_what_it_is_is_left_alone() -> None:
    """A named control does not need its decoration described -- that is noise
    on every icon button on the page."""

    button = _node("button", 2, ax_role="button", ax_name="Add to cart",
                   bounds=BoundingBox(0, 0, 80, 80))
    _child(button, _node("img", 3, attrs={"alt": "shopping bag icon"}))
    body = _node("body", 1)
    _child(body, button)

    text = serialize(body).llm_text
    assert "Add to cart" in text
    assert "image_alt" not in text


def test_the_image_description_is_capped() -> None:
    button = _node("button", 2, bounds=BoundingBox(0, 0, 80, 80))
    _child(button, _node("img", 3, attrs={"alt": "x" * 400}))
    body = _node("body", 1)
    _child(body, button)

    line = serialize(body).llm_text
    assert "…" in line
    assert len(line) < 200


# ------------------------------------------------------- authoring view
#
# The default render is built for an agent that CLICKS: interactive elements get
# a `[ref]` line and everything else contributes a bare line of text. A Zara
# product page serialized that way is one `[e169]<div id=app-root />` followed by
# nine thousand characters of unattributed text -- `class=` appears zero times in
# the whole document.
#
# The selector agent is asked, from that, to propose CSS. It has no id, no class,
# no tag and no nesting to name, so every proposal is a guess and "no proposed
# candidate resolved to a value" is the only possible outcome for anything not
# already in the page's JSON-LD.


def _care_panel() -> EnhancedDOMTreeNode:
    body = _node("body", 1)
    panel = _child(body, _node("div", 2, attrs={"class": "product-detail-care"}))
    _child(panel, _node("#text", 3, node_type=NodeType.TEXT_NODE, value="Do not wash"))
    return body


def test_the_default_render_shows_no_structure() -> None:
    """Asserted so the authoring view's reason for existing stays visible."""

    text = serialize(_care_panel()).llm_text
    assert "Do not wash" in text
    assert "class" not in text


def test_the_authoring_view_gives_the_text_a_handle() -> None:
    text = serialize(_care_panel(), view=SnapshotView(for_authoring=True)).llm_text
    assert "class=product-detail-care" in text
    assert "Do not wash" in text


def test_an_unaddressable_wrapper_earns_no_line() -> None:
    """An element carrying nothing selectable cannot be pointed at, so a line
    for it costs the budget and buys the model nothing."""

    body = _node("body", 1)
    panel = _child(body, _node("div", 2))
    _child(panel, _node("#text", 3, node_type=NodeType.TEXT_NODE, value="Do not wash"))

    text = serialize(body, view=SnapshotView(for_authoring=True)).llm_text
    assert "<div" not in text
    assert "Do not wash" in text


def test_only_elements_that_directly_hold_text_are_rendered() -> None:
    """A wrapper whose text all lives three levels down is not where a selector
    should point, and rendering every ancestor buries the page in scaffolding."""

    body = _node("body", 1)
    outer = _child(body, _node("section", 2, attrs={"class": "outer"}))
    inner = _child(outer, _node("p", 3, attrs={"class": "inner"}))
    _child(inner, _node("#text", 4, node_type=NodeType.TEXT_NODE, value="Made in China"))

    text = serialize(body, view=SnapshotView(for_authoring=True)).llm_text
    assert "class=inner" in text
    assert "class=outer" not in text


def test_the_authoring_view_is_off_for_the_agent_loop() -> None:
    """The agent is choosing what to press; class names are noise there, which
    is why browser-use leaves `class` out of the default whitelist."""

    text = serialize(_care_panel(), view=SnapshotView(visible_text_only=True)).llm_text
    assert "class" not in text
