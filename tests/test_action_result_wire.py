"""`routes/sessions.py`'s response conversion, at the route's own layer.

`crawlpilot.wire` owns the shape and `packages/crawlpilot/tests/test_wire.py`
tests it in isolation. What is left here is the platform's half: that the
converter the route actually calls serializes a *fused tree* -- what the driver
really returns -- rather than the pre-serialized snapshots a unit test finds
convenient, and that the two fields which must not cross the network do not.

Unit-level against the route function, no `TestClient`, per this repo's
convention (see `test_sessions_list.py`).
"""

from __future__ import annotations

from fusion_fixtures import fnode

from agentpilot.gateway.routes.sessions import _to_action_result_out
from crawlpilot.spi.actions import ActionResult, TabInfo
from crawlpilot.spi.geometry import BoundingBox


def _tree():  # type: ignore[no-untyped-def]
    return fnode(
        "main",
        children=[
            fnode("button", "Buy now", "e12", bbox=BoundingBox(10, 20, 80, 30)),
            fnode("textbox", "Search", "e13", bbox=BoundingBox(0, 0, 200, 24)),
        ],
    )


def test_a_driver_returned_tree_is_serialized_on_the_way_out() -> None:
    """The production path. The driver populates `fused_trees`; nothing has
    serialized them yet, so the route must -- and until this projection existed,
    that serialization lived in the route as a hand-written converter.
    """

    out = _to_action_result_out(ActionResult(fused_trees=[_tree()])).model_dump()

    snapshot = out["snapshots"][0]
    assert '[e12]<button "Buy now" />' in snapshot["llm_text"]
    assert snapshot["refs"]["e12"]["role"] == "button"
    assert snapshot["refs"]["e12"]["name"] == "Buy now"
    assert snapshot["refs"]["e12"]["bbox"] == {"x": 10, "y": 20, "width": 80, "height": 30}


def test_the_tree_itself_never_reaches_the_wire() -> None:
    """`fused_trees` is the whole fused DOM -- parent back-references included,
    which do not survive JSON at all. `snapshots` is what a remote caller gets.
    """

    out = _to_action_result_out(ActionResult(fused_trees=[_tree()])).model_dump()
    assert "fused_trees" not in out
    assert "snapshot_views" not in out


def test_the_route_carries_the_fields_the_old_converter_dropped() -> None:
    """The regression this whole change exists for: `values` is what
    `api.BrowserSession`'s getters read, and the hand-written converter never
    emitted it -- so no client on the far side of this route could implement
    `get_text`, `is_visible` or `get_count`.
    """

    result = ActionResult(
        fused_trees=[_tree()],
        values=[True],
        readouts=["#buy is visible"],
        verifications=["clicked #buy"],
        page_title="Shop",
        status_code=200,
        pdfs=[b"%PDF-x"],
        tabs=[[TabInfo(page_id="p1", url="http://x", title="X", active=True)]],
    )
    out = _to_action_result_out(result).model_dump()

    assert out["values"] == [True]
    assert out["readouts"] == ["#buy is visible"]
    assert out["verifications"] == ["clicked #buy"]
    assert out["page_title"] == "Shop"
    assert out["status_code"] == 200
    assert out["pdfs"] == ["JVBERi14"]  # base64, as screenshots already were
    assert out["tabs"][0][0]["page_id"] == "p1"
