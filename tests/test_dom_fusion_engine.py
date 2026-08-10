"""Unit tests for the pure fusion builder in
`agentpilot.driver.dom_fusion_engine` -- AX flattening, attribute conversion,
shadow/child separation, iframe coordinate offset, and viewport visibility.
Synthetic CDP payloads only; no browser, no async."""

from __future__ import annotations

from agentpilot.driver.dom_fusion import LayoutInfo
from agentpilot.driver.dom_fusion_engine import build_ax_lookup, build_enhanced_tree
from agentpilot.spi.dom_tree import NodeType
from agentpilot.spi.snapshot import BoundingBox


def _rect(x: float, y: float, w: float, h: float) -> BoundingBox:
    return BoundingBox(x=x, y=y, width=w, height=h)


def _visible_layout(bounds: BoundingBox) -> LayoutInfo:
    return LayoutInfo(bounds=bounds, computed_styles={"display": "block", "visibility": "visible"})


def test_build_ax_lookup_flattens_and_filters() -> None:
    ax_tree = {
        "nodes": [
            {
                "backendDOMNodeId": 100,
                "role": {"value": "button"},
                "name": {"value": "Buy"},
                "properties": [
                    {"name": "focusable", "value": {"value": True}},
                    {"name": "keyshortcuts", "value": {"value": "Enter"}},
                    {"name": "someRandomProp", "value": {"value": "x"}},  # dropped
                ],
            },
            {"role": {"value": "ignored"}},  # no backendDOMNodeId -> skipped
        ]
    }
    lookup = build_ax_lookup(ax_tree)
    assert set(lookup) == {100}
    node = lookup[100]
    assert node.role == "button"
    assert node.name == "Buy"
    assert node.properties == {"focusable": True, "keyshortcuts": "Enter"}


def _document() -> dict:
    # #document > HTML(frame) > BODY > [BUTTON#buy, DIV(shadow: SPAN)]
    return {
        "root": {
            "nodeId": 1,
            "backendNodeId": 1,
            "nodeType": NodeType.DOCUMENT_NODE.value,
            "nodeName": "#document",
            "children": [
                {
                    "nodeId": 2,
                    "backendNodeId": 2,
                    "nodeType": NodeType.ELEMENT_NODE.value,
                    "nodeName": "HTML",
                    "frameId": "main",
                    "children": [
                        {
                            "nodeId": 3,
                            "backendNodeId": 3,
                            "nodeType": NodeType.ELEMENT_NODE.value,
                            "nodeName": "BODY",
                            "children": [
                                {
                                    "nodeId": 4,
                                    "backendNodeId": 100,
                                    "nodeType": NodeType.ELEMENT_NODE.value,
                                    "nodeName": "BUTTON",
                                    "attributes": ["id", "buy", "class", "btn"],
                                    "children": [],
                                },
                                {
                                    "nodeId": 5,
                                    "backendNodeId": 101,
                                    "nodeType": NodeType.ELEMENT_NODE.value,
                                    "nodeName": "DIV",
                                    "shadowRoots": [
                                        {
                                            "nodeId": 6,
                                            "backendNodeId": 200,
                                            "nodeType": NodeType.DOCUMENT_FRAGMENT_NODE.value,
                                            "nodeName": "#document-fragment",
                                            "shadowRootType": "open",
                                            "children": [
                                                {
                                                    "nodeId": 7,
                                                    "backendNodeId": 201,
                                                    "nodeType": NodeType.ELEMENT_NODE.value,
                                                    "nodeName": "SPAN",
                                                    "children": [],
                                                }
                                            ],
                                        }
                                    ],
                                    # The shadow root also appears in children and
                                    # must be filtered out of children_nodes.
                                    "children": [
                                        {
                                            "nodeId": 6,
                                            "backendNodeId": 200,
                                            "nodeType": NodeType.DOCUMENT_FRAGMENT_NODE.value,
                                            "nodeName": "#document-fragment",
                                        }
                                    ],
                                },
                            ],
                        }
                    ],
                }
            ],
        }
    }


def test_build_enhanced_tree_structure_attrs_and_ax() -> None:
    snapshot = {100: _visible_layout(_rect(0, 0, 80, 30))}
    ax = build_ax_lookup(
        {
            "nodes": [
                {"backendDOMNodeId": 100, "role": {"value": "button"}, "name": {"value": "Buy"}}
            ]
        }
    )
    root = build_enhanced_tree(_document(), snapshot, ax, target_id="t", session_id="s")

    html = root.children_nodes[0]
    body = html.children_nodes[0]
    button = body.children_nodes[0]
    div = body.children_nodes[1]

    assert button.attributes == {"id": "buy", "class": "btn"}
    assert button.ax_name == "Buy" and button.ax_role == "button"
    assert button.parent_node is body and button.target_id == "t" and button.session_id == "s"
    assert button.is_visible is True

    # Shadow root separated from children.
    assert div.children_nodes == []
    assert len(div.shadow_roots) == 1
    assert div.shadow_roots[0].shadow_root_type == "open"
    assert div.children_and_shadow_roots[0].node_name == "#document-fragment"


def test_visibility_css_gate() -> None:
    hidden = {100: LayoutInfo(bounds=_rect(0, 0, 10, 10), computed_styles={"display": "none"})}
    root = build_enhanced_tree(_document(), hidden, {})
    button = root.children_nodes[0].children_nodes[0].children_nodes[0]
    assert button.is_visible is False

    no_bounds = {100: LayoutInfo(bounds=None, computed_styles={"display": "block"})}
    root2 = build_enhanced_tree(_document(), no_bounds, {})
    button2 = root2.children_nodes[0].children_nodes[0].children_nodes[0]
    assert button2.is_visible is False


def test_iframe_offset_applied_to_absolute_position() -> None:
    # #document > IFRAME(bounds 50,60) > contentDocument HTML > DIV(bounds 10,10)
    doc = {
        "root": {
            "nodeId": 1,
            "backendNodeId": 1,
            "nodeType": NodeType.DOCUMENT_NODE.value,
            "nodeName": "#document",
            "children": [
                {
                    "nodeId": 2,
                    "backendNodeId": 2,
                    "nodeType": NodeType.ELEMENT_NODE.value,
                    "nodeName": "IFRAME",
                    "contentDocument": {
                        "nodeId": 3,
                        "backendNodeId": 3,
                        "nodeType": NodeType.ELEMENT_NODE.value,
                        "nodeName": "HTML",
                        "frameId": "child",
                        "children": [
                            {
                                "nodeId": 4,
                                "backendNodeId": 300,
                                "nodeType": NodeType.ELEMENT_NODE.value,
                                "nodeName": "DIV",
                                "children": [],
                            }
                        ],
                    },
                }
            ],
        }
    }
    snapshot = {
        2: LayoutInfo(bounds=_rect(50, 60, 400, 300)),
        300: _visible_layout(_rect(10, 10, 20, 20)),
    }
    root = build_enhanced_tree(doc, snapshot, {})
    iframe = root.children_nodes[0]
    div = iframe.content_document.children_nodes[0]
    # Inner bounds (10,10) shifted by the iframe origin (50,60) -> document space.
    assert div.absolute_position is not None
    assert (div.absolute_position.x, div.absolute_position.y) == (60.0, 70.0)


def test_memoization_returns_same_instance() -> None:
    root = build_enhanced_tree(_document(), {}, {})
    body = root.children_nodes[0].children_nodes[0]
    # parent back-reference and forward child reference are the same object.
    assert body.children_nodes[0].parent_node is body
