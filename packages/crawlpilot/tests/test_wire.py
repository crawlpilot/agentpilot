"""`crawlpilot.wire` -- the HTTP form of `ActionResult`.

Lives in crawlpilot's own suite (which CI runs in a venv with *only* crawlpilot
installed) because the shape is crawlpilot's, not the platform's: a server and a
client both encode against it, and neither owns it.
"""

from __future__ import annotations

import dataclasses

import pytest

from crawlpilot.spi.actions import (
    ActionResult,
    DialogInfo,
    DownloadInfo,
    FrameInfo,
    TabInfo,
)
from crawlpilot.spi.dom_tree import RefInfo, Snapshot
from crawlpilot.spi.geometry import BoundingBox
from crawlpilot.wire import ActionResultWire, from_wire, to_wire


def _populated() -> ActionResult:
    """Every field set to something distinguishable from its default.

    Deliberately exhaustive: the failure this guards against is a field being
    silently *absent*, which a partially-populated fixture cannot catch -- and
    which is exactly what the hand-written converter did to `values`, `readouts`,
    `verifications`, `pdfs`, `frames` and `page_title`.
    """

    return ActionResult(
        snapshots=[
            Snapshot(
                llm_text='[e1]<button "Buy"/>',
                refs={"e1": RefInfo(role="button", name="Buy", bbox=BoundingBox(1, 2, 3, 4))},
            )
        ],
        screenshots=[b"\x89PNG-not-really"],
        pdfs=[b"%PDF-not-really"],
        downloads=[
            DownloadInfo(path="/tmp/a.csv", filename="a.csv", size_bytes=12, url="http://x/a.csv")
        ],
        frames=[[FrameInfo(frame_id="f1", url="http://x", name="n", is_main=True)]],
        extracts=["# hello"],
        js_returns=[{"a": 1}],
        tabs=[[TabInfo(page_id="p1", url="http://x", title="X", active=True)]],
        page_title="X",
        verifications=["clicked #buy"],
        readouts=["count: 3 element(s) match '.item'"],
        values=[3],
        sequence_aborted=True,
        page_changed=True,
        status_code=200,
        soft_verdict="too_small",
        soft_weight=2,
        dialog=DialogInfo(kind="confirm", message="sure?", default_value=""),
    )


def test_every_field_round_trips() -> None:
    result = _populated()
    assert from_wire(to_wire(result)) == result


def test_the_wire_carries_every_dataclass_field_except_the_local_only_two() -> None:
    """The regression that motivated this module: the hand-written response
    model named eight fields and the dataclass had eighteen.

    Asserted against `dataclasses.fields` rather than a hardcoded list, so a
    field added to `ActionResult` and forgotten here fails immediately.
    """

    local_only = {"fused_trees", "snapshot_views"}
    expected = {f.name for f in dataclasses.fields(ActionResult)} - local_only
    assert set(ActionResultWire.model_fields) == expected


@pytest.mark.parametrize(
    "field, value",
    [
        ("values", [3]),
        ("readouts", ["count: 3 element(s) match '.item'"]),
        ("verifications", ["clicked #buy"]),
        ("page_title", "X"),
        ("status_code", 200),
        ("pdfs", [b"%PDF-not-really"]),
    ],
)
def test_the_fields_the_old_converter_dropped(field: str, value: object) -> None:
    """Named individually because each one disabled a specific client method:
    without `values` a remote `is_visible()` cannot return a bool, and returned
    the prose `"#buy is not visible"` -- a non-empty, therefore truthy, string.
    """

    assert getattr(from_wire(to_wire(_populated())), field) == value


# ------------------------------------------------------- forward/backward skew


def test_an_older_client_ignores_a_field_it_has_never_heard_of() -> None:
    """`extra="ignore"` on responses. With `"forbid"` -- which is right for a
    *request* -- a purely additive server change would crash every older client.
    """

    result = from_wire({"extracts": ["hi"], "values": [3], "a_field_from_a_later_version": {}})
    assert result.extracts == ["hi"]
    assert result.values == [3]


def test_a_newer_client_fills_defaults_for_a_field_an_older_server_omits() -> None:
    result = from_wire({"extracts": ["hi"]})
    assert result.values == []
    assert result.pdfs == []
    assert result.dialog is None


def test_the_pre_versioning_name_for_snapshots_still_decodes() -> None:
    """`snapshots` was called `fused_trees` on the wire before this module, and
    never carried a fused tree -- always this serialized view. The name is
    corrected; the old one stays accepted so no existing payload breaks."""

    old = from_wire({"fused_trees": [{"llm_text": "x", "refs": {}}]})
    new = from_wire({"snapshots": [{"llm_text": "x", "refs": {}}]})
    assert old == new
    assert old.snapshots[0].llm_text == "x"


# ------------------------------------------------------------------ encoding


def test_bytes_travel_as_base64_and_come_back_as_bytes() -> None:
    wire = to_wire(ActionResult(screenshots=[b"\x00\x01\x02"], pdfs=[b"\x03\x04"]))
    assert isinstance(wire.model_dump()["screenshots"][0], str)
    assert from_wire(wire).screenshots == [b"\x00\x01\x02"]
    assert from_wire(wire).pdfs == [b"\x03\x04"]


def test_a_tree_with_no_serializer_raises_rather_than_dropping_perception() -> None:
    """Silently emitting an empty `snapshots` would surface much later as an
    agent staring at a page it believes is blank."""

    result = ActionResult(fused_trees=[object()])  # type: ignore[list-item]
    with pytest.raises(ValueError, match="serialize_tree"):
        to_wire(result)


def test_a_result_that_already_carries_snapshots_needs_no_serializer() -> None:
    result = ActionResult(snapshots=[Snapshot(llm_text="x")])
    assert to_wire(result).model_dump()["snapshots"][0]["llm_text"] == "x"


def test_the_local_only_fields_never_reach_the_wire() -> None:
    dumped = to_wire(ActionResult(snapshots=[Snapshot(llm_text="x")])).model_dump()
    assert "fused_trees" not in dumped
    assert "snapshot_views" not in dumped
