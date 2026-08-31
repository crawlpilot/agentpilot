"""The HTTP form of `spi.actions.ActionResult`, projected from the dataclass.

Requests have been a projection since the `ToolSpec` consolidation: one
`tools/catalog.py` entry yields the wire model, the agent model, the JSON schema
and the provider adapters. Responses were not. `gateway/schemas.py` carried a
hand-written `ActionResultOut` and `routes/sessions.py` a hand-written converter
into it, and between them they silently dropped six fields the dataclass
already had -- `values`, `readouts`, `verifications`, `pdfs`, `frames` and
`page_title`.

That was not cosmetic. `values` is what `api.BrowserSession`'s getters read, so
its absence made the HTTP API *strictly weaker than the library*: no caller on
the far side of it could implement `get_text`, `is_visible`, `get_count`,
`dropdown_options`, `pdf()` or `list_frames()` at all. A remote session that is
the same object as a local one is impossible while the response is a lossy
hand-maintained copy, which is why this module exists.

**Owned by crawlpilot, not by the platform.** The server and any client both
import it, so the two cannot disagree about the shape; a platform-side copy
would be the same drift one layer up.

**Serialization, never transport.** Nothing here imports httpx, fastapi, or
`crawlpilot.dom` -- `to_wire` takes the tree serializer as an argument instead.
An import-linter contract holds that line, and it is what lets a client depend
on this module without acquiring an HTTP framework or a DOM pipeline.

**Strictness is directional.** Requests are `extra="forbid"`: a server rejecting
an unknown field is a feature. Responses are `extra="ignore"`, because the same
strictness on a *reply* turns a purely additive server change into a client
crash -- an older client decoding a newer server's result would hard-fail on a
field it simply does not need. See `WIRE_API_VERSION`.
"""

from __future__ import annotations

import base64
import dataclasses
from collections.abc import Callable
from typing import Any, Protocol

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, create_model

from crawlpilot._modelgen import dataclass_fields
from crawlpilot.spi.actions import (
    ActionResult,
    DialogInfo,
    DownloadInfo,
    FrameInfo,
    TabInfo,
)
from crawlpilot.spi.dom_tree import EnhancedDOMTreeNode, RefInfo, Snapshot, SnapshotView
from crawlpilot.spi.geometry import BoundingBox

WIRE_API_VERSION = "1.0"
"""The version of *this* representation, independent of either package's release
version.

Bump the **major** only when a field is removed or changes type -- the two
things a decoder cannot absorb. Adding a field is not a bump: `extra="ignore"`
below means an older client already tolerates one, which is the entire reason
that setting is not `"forbid"`.

A client compares its own value against the server's and applies
`extensions.manifest.check_compatibility`, whose asymmetry is exactly right here
too: older-than-host loads with a warning, newer-than-host is refused rather
than failing deep inside a call later.
"""


# --------------------------------------------------------------------- helpers


def _model_from(
    cls: type,
    *,
    name: str,
    overrides: dict[str, tuple[Any, Any]] | None = None,
    exclude: frozenset[str] = frozenset(),
    localns: dict[str, Any] | None = None,
) -> type[BaseModel]:
    """A response model projected from a dataclass.

    `extra="ignore"`, unlike `ToolSpec.wire_model()`'s `extra="forbid"` -- see
    the module docstring on why strictness has a direction.
    """

    fields = dataclass_fields(cls, localns=localns)
    fields.update(overrides or {})
    model: type[BaseModel] = create_model(
        name,
        # `populate_by_name` so a field carrying a `validation_alias` still
        # accepts its real name -- see `snapshots`, which answers to both.
        __config__=ConfigDict(extra="ignore", populate_by_name=True),
        **{k: v for k, v in fields.items() if k not in exclude},  # type: ignore[call-overload]
    )
    return model


def _list_of(model: Any) -> Any:
    """`list[model]`, built at runtime.

    A generated model is a *variable* to a type checker, not a class, so
    `list[SnapshotWire]` written literally is rejected as an annotation even
    though it is exactly right at runtime. Going through a function typed `Any`
    says the quiet part honestly -- these shapes are only known at import time --
    instead of scattering `# type: ignore[valid-type]` down the file.
    """

    return list[model]


def _list_of_lists(model: Any) -> Any:
    return list[list[model]]


def _dict_of(model: Any) -> Any:
    return dict[str, model]


def _optional(model: Any) -> Any:
    return model | None


# ------------------------------------------------------------- nested models
#
# Each is projected from the dataclass it mirrors, so adding a field to
# `TabInfo` or `FrameInfo` reaches the wire with no edit here.

BoundingBoxWire = _model_from(BoundingBox, name="BoundingBoxWire")
RefInfoWire = _model_from(
    RefInfo, name="RefInfoWire", overrides={"bbox": (_optional(BoundingBoxWire), None)}
)
SnapshotWire = _model_from(
    Snapshot,
    name="SnapshotWire",
    overrides={"refs": (_dict_of(RefInfoWire), Field(default_factory=dict))},
)
FrameInfoWire = _model_from(FrameInfo, name="FrameInfoWire")
TabInfoWire = _model_from(TabInfo, name="TabInfoWire")
DownloadWire = _model_from(DownloadInfo, name="DownloadWire")
DialogInfoWire = _model_from(DialogInfo, name="DialogInfoWire")


# ----------------------------------------------------------- the result model

_LOCAL_ONLY = frozenset({"fused_trees", "snapshot_views"})
"""The two fields that deliberately do not cross the network.

`fused_trees` is the whole fused DOM -- every node, every attribute, parent
back-references that do not survive JSON at all -- and `snapshot_views` is the
filter set the driver already applied when producing it. `snapshots` carries
what a remote caller can actually use, and `to_wire` fills it from these. See
`spi.dom_tree.Snapshot`.
"""

_OVERRIDES: dict[str, tuple[Any, Any]] = {
    # Bytes are not JSON. Base64, matching what the hand-written model did for
    # screenshots -- `pdfs` simply never reached the wire before.
    "screenshots": (list[str], Field(default_factory=list)),
    "pdfs": (list[str], Field(default_factory=list)),
    # Emitted as `snapshots`; also *accepted* as `fused_trees`, the name this
    # field had before there was a versioned wire. The old name never described
    # what it carried -- it has always held the serialized `{llm_text, refs}`
    # view, never a fused tree, which is precisely why it is being corrected --
    # and honouring it on input means no existing payload stops decoding.
    "snapshots": (
        _list_of(SnapshotWire),
        Field(default_factory=list, validation_alias=AliasChoices("snapshots", "fused_trees")),
    ),
    "downloads": (_list_of(DownloadWire), Field(default_factory=list)),
    "frames": (_list_of_lists(FrameInfoWire), Field(default_factory=list)),
    "tabs": (_list_of_lists(TabInfoWire), Field(default_factory=list)),
    "dialog": (_optional(DialogInfoWire), None),
    # `list[object]` is not a useful annotation to hand Pydantic; these carry
    # whatever the page returned.
    "js_returns": (list[Any], Field(default_factory=list)),
    "values": (list[Any], Field(default_factory=list)),
}

ActionResultWire = _model_from(
    ActionResult,
    name="ActionResultWire",
    overrides=_OVERRIDES,
    exclude=_LOCAL_ONLY,
    # `ActionResult` annotates these but imports none of them at runtime.
    localns={
        "EnhancedDOMTreeNode": EnhancedDOMTreeNode,
        "Snapshot": Snapshot,
        "SnapshotView": SnapshotView,
    },
)


# ------------------------------------------------------------- serialization


class SerializedDOMLike(Protocol):
    """What `to_wire` needs back from a tree serializer: the rendered text and
    the ref -> node map. Structural rather than an import of
    `dom.serializer.SerializedDOM`, so this module stays free of `crawlpilot.dom`
    and a client never pulls the DOM pipeline in to decode a reply."""

    llm_text: str
    selector_map: Any


TreeSerializer = Callable[[Any, SnapshotView | None], SerializedDOMLike]
"""`dom.serializer.serialize`, passed in rather than imported."""


def snapshot_of(tree: Any, view: SnapshotView | None, serialize: TreeSerializer) -> Snapshot:
    """One fused tree, serialized into the form that crosses a network.

    This is `routes/sessions.py::_fused_tree_out`, moved down to the layer that
    owns the shape. It stayed on the platform side only because the wire model
    did, and the reshaping below is the same on any transport.
    """

    serialized = serialize(tree, view)
    refs: dict[str, RefInfo] = {}
    for backend_node_id, node in serialized.selector_map.items():
        refs[f"e{backend_node_id}"] = RefInfo(
            role=node.ax_role, name=node.ax_name, bbox=node.absolute_position
        )
    return Snapshot(llm_text=serialized.llm_text, refs=refs)


def to_wire(result: ActionResult, *, serialize_tree: TreeSerializer | None = None) -> Any:
    """`ActionResult` -> its HTTP form.

    `serialize_tree` is only consulted for trees that have not already been
    serialized: if `result.snapshots` is populated the caller has done it, and
    if `fused_trees` is empty there is nothing to do. A result carrying trees
    with no serializer raises rather than silently dropping the perception --
    a snapshot that vanished on the way out is a bug that would otherwise
    surface as an agent staring at an empty page.
    """

    snapshots = list(result.snapshots)
    if not snapshots and result.fused_trees:
        if serialize_tree is None:
            raise ValueError(
                "result carries fused_trees but no snapshots and no serialize_tree "
                "was given; pass crawlpilot.dom.serializer.serialize"
            )
        views: list[SnapshotView | None] = list(result.snapshot_views)
        views += [None] * (len(result.fused_trees) - len(views))
        snapshots = [
            snapshot_of(tree, view, serialize_tree)
            for tree, view in zip(result.fused_trees, views, strict=True)
            if tree is not None
        ]

    source = {f: getattr(result, f) for f in ActionResultWire.model_fields}
    source["snapshots"] = snapshots
    return ActionResultWire(**{k: _encode(v) for k, v in source.items()})


def _encode(value: Any) -> Any:
    """Anything the dataclass holds -> something JSON and Pydantic accept.

    Recursive and type-driven rather than a per-field table: a field added to
    `TabInfo`, `Snapshot` or `ActionResult` itself is carried with no edit here,
    which is the whole point of projecting the response instead of hand-writing
    it. `bytes` is the one primitive that needs a decision, and base64 is the one
    the hand-written model already made for screenshots.
    """

    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _encode(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, (list, tuple)):
        return [_encode(v) for v in value]
    if isinstance(value, dict):
        return {k: _encode(v) for k, v in value.items()}
    return value


def from_wire(payload: Any) -> ActionResult:
    """Its HTTP form -> `ActionResult`.

    The inverse of `to_wire` in every field that can round-trip. `fused_trees`
    and `snapshot_views` come back empty, necessarily: the tree was never sent.
    `snapshots` carries the perception instead, which is what a caller reads.

    Unknown keys are dropped (`extra="ignore"`) and absent ones take the
    dataclass's own defaults, so this decodes a reply from a newer *or* an older
    server without either side negotiating.
    """

    parsed = payload if isinstance(payload, BaseModel) else ActionResultWire(**dict(payload))
    data = parsed.model_dump()

    # Decode the fields whose wire form differs, then hand the untouched
    # remainder straight to the dataclass -- so a field added to `ActionResult`
    # arrives here with no edit, which is the property this module exists for.
    snapshots = data.pop("snapshots", [])
    screenshots = data.pop("screenshots", [])
    pdfs = data.pop("pdfs", [])
    downloads = data.pop("downloads", [])
    frames = data.pop("frames", [])
    tabs = data.pop("tabs", [])
    dialog = data.pop("dialog", None)

    return ActionResult(
        snapshots=[_snapshot_from(s) for s in snapshots],
        screenshots=[base64.b64decode(s) for s in screenshots],
        pdfs=[base64.b64decode(s) for s in pdfs],
        downloads=[DownloadInfo(**d) for d in downloads],
        frames=[[FrameInfo(**f) for f in group] for group in frames],
        tabs=[[TabInfo(**t) for t in group] for group in tabs],
        dialog=DialogInfo(**dialog) if dialog is not None else None,
        **data,
    )


def _snapshot_from(payload: dict[str, Any]) -> Snapshot:
    return Snapshot(
        llm_text=payload["llm_text"],
        refs={
            ref: RefInfo(
                role=info["role"],
                name=info["name"],
                bbox=BoundingBox(**info["bbox"]) if info.get("bbox") else None,
            )
            for ref, info in (payload.get("refs") or {}).items()
        },
    )


__all__ = [
    "WIRE_API_VERSION",
    "ActionResultWire",
    "BoundingBoxWire",
    "DialogInfoWire",
    "DownloadWire",
    "FrameInfoWire",
    "RefInfoWire",
    "SnapshotWire",
    "TabInfoWire",
    "TreeSerializer",
    "from_wire",
    "snapshot_of",
    "to_wire",
]
