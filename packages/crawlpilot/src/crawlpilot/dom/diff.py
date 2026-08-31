"""Cross-step DOM change identification -- the headline of the fusion port.

Lives in `crawlpilot.dom` rather than the platform because it never needed
anything else: it imports `dom.clickable_elements` and `spi.dom_tree` and
nothing more, so the standalone library was shipping the fused tree while
keeping the one thing that makes a *sequence* of them readable in a package its
users do not install. `diff_snapshot` is the verb built on it.

Instead of re-sending the whole page and making the model re-derive what changed
(a `*`-only, `role:name`-path marking of new elements), `diff_snapshots`
compares the interactive elements of two fused trees and produces a typed change
report: **NEW / REMOVED / MOVED / MODIFIED**. Rendered as a compact "changes
since last step" block, this is both the clearest signal for the agent and the
biggest per-step token saving (a delta instead of a full re-read).

Identity model (mirrors browser-use's `(session_id, backend_node_id)`
set-difference for NEW, generalized): elements are matched across steps by that
same pair -- stable within a document -- with `stable_hash` (structure + static
attrs + accessible name, dynamic CSS classes filtered) as a fallback that
absorbs the backend-id reassignment a re-render can cause. Only after both keys
fail to match is an element considered genuinely NEW/REMOVED.

The `session_id` half is load-bearing once cross-origin iframes are captured:
`backendNodeId` is unique per renderer, so a bare id would let an element in an
embedded frame match an unrelated element in the main document and report a real
change as "unchanged". browser-use makes the same point at
`dom/serializer/serializer.py:436` -- "CDP node IDs are scoped to a session and
can be reused by unrelated elements in cross-origin iframe targets".

- **NEW**: a current interactive element that matched nothing in the previous step.
- **REMOVED**: a previous interactive element that matched nothing now.
- **MOVED**: same element (matched), different `parent_branch_hash` (re-parented / reordered).
- **MODIFIED**: same element, changed observable state -- accessible name, field
  value, or an AX state flag (checked / expanded / pressed / selected / disabled).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import Enum

from crawlpilot.dom.clickable_elements import is_interactive
from crawlpilot.spi.dom_tree import EnhancedDOMTreeNode

_STATE_PROPS = ("checked", "expanded", "pressed", "selected", "disabled")

NodeIdentity = tuple[str | None, int]
"""`(session_id, backend_node_id)` -- see the module docstring on why the bare
backend id is not enough once more than one renderer is in play."""


def _identity(node: EnhancedDOMTreeNode) -> NodeIdentity:
    return (node.session_id, node.backend_node_id)


class ChangeKind(Enum):
    NEW = "new"
    REMOVED = "removed"
    MOVED = "moved"
    MODIFIED = "modified"


@dataclass(frozen=True)
class DomChange:
    kind: ChangeKind
    node: EnhancedDOMTreeNode
    """The current node for NEW/MOVED/MODIFIED; the previous node for REMOVED."""
    detail: str = ""
    """Human-readable specifics, e.g. ``value: '' -> 'a@b.com'`` for MODIFIED."""


@dataclass
class DomDiff:
    changes: list[DomChange] = field(default_factory=list)
    new_backend_ids: set[int] = field(default_factory=set)
    """Backend ids of NEW interactive nodes -- the inline `*` marker set the
    serializer consumes."""

    @property
    def has_changes(self) -> bool:
        return bool(self.changes)

    def of_kind(self, kind: ChangeKind) -> list[DomChange]:
        return [c for c in self.changes if c.kind is kind]


def iter_interactive(root: EnhancedDOMTreeNode) -> Iterator[EnhancedDOMTreeNode]:
    """Depth-first over the fused tree (children + shadow roots + iframe content
    documents), yielding interactive nodes that aren't explicitly hidden. Parent
    back-references are never followed, so this can't cycle."""

    stack: list[EnhancedDOMTreeNode] = [root]
    seen: set[int] = set()
    while stack:
        node = stack.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        if node.is_visible is not False and is_interactive(node):
            yield node
        if node.content_document is not None:
            stack.append(node.content_document)
        stack.extend(node.children_and_shadow_roots)


def _value_of(node: EnhancedDOMTreeNode) -> str:
    """Best-effort current value of a control: AX `valuetext`, then the `value`
    attribute. Used to surface filled-field changes as MODIFIED."""

    if node.ax_node is not None:
        # The live AX value first: it is the only one that reflects what a user
        # (or a `fill`) actually typed. The `value` attribute below is the
        # *initial* value and never changes, so relying on it made every filled
        # field look untouched.
        if node.ax_node.value:
            return node.ax_node.value
        vt = node.ax_node.properties.get("valuetext")
        if isinstance(vt, str) and vt:
            return vt
    return node.attributes.get("value", "")


def _state_signature(node: EnhancedDOMTreeNode) -> tuple[object, ...]:
    """The observable state that, when changed on an otherwise-matched element,
    counts as MODIFIED: accessible name, value, and the AX state flags."""

    props = node.ax_node.properties if node.ax_node is not None else {}
    return (node.ax_name, _value_of(node), *(props.get(p) for p in _STATE_PROPS))


def _describe_modification(prev: EnhancedDOMTreeNode, curr: EnhancedDOMTreeNode) -> str:
    parts: list[str] = []
    if prev.ax_name != curr.ax_name:
        parts.append(f"name: {prev.ax_name!r} -> {curr.ax_name!r}")
    pv, cv = _value_of(prev), _value_of(curr)
    if pv != cv:
        parts.append(f"value: {pv!r} -> {cv!r}")
    pp = prev.ax_node.properties if prev.ax_node is not None else {}
    cp = curr.ax_node.properties if curr.ax_node is not None else {}
    for prop in _STATE_PROPS:
        if pp.get(prop) != cp.get(prop):
            parts.append(f"{prop}: {pp.get(prop)!r} -> {cp.get(prop)!r}")
    return "; ".join(parts)


def diff_snapshots(
    previous: EnhancedDOMTreeNode | None,
    current: EnhancedDOMTreeNode,
) -> DomDiff:
    """Compare the interactive elements of two fused trees. With no previous
    tree (first observation / post-navigation) returns an empty diff -- there is
    nothing to delta against, so the caller renders the full tree."""

    diff = DomDiff()
    current_nodes = list(iter_interactive(current))
    if previous is None:
        return diff

    previous_nodes = list(iter_interactive(previous))
    prev_by_backend = {_identity(n): n for n in previous_nodes}
    cur_by_backend = {_identity(n): n for n in current_nodes}

    matched_prev: set[NodeIdentity] = set()  # identities consumed by a match
    matched_cur: set[NodeIdentity] = set()

    # Tier 1: match by stable (session_id, backend_node_id).
    for identity, curr in cur_by_backend.items():
        prev = prev_by_backend.get(identity)
        if prev is None:
            continue
        matched_prev.add(identity)
        matched_cur.add(identity)
        _classify_match(prev, curr, diff)

    # Tier 2: absorb backend-id reassignment -- match leftovers by stable_hash.
    prev_by_hash: dict[int, list[EnhancedDOMTreeNode]] = {}
    for n in previous_nodes:
        if _identity(n) not in matched_prev:
            prev_by_hash.setdefault(n.stable_hash(), []).append(n)

    still_new: list[EnhancedDOMTreeNode] = []
    for curr in current_nodes:
        if _identity(curr) in matched_cur:
            continue
        bucket = prev_by_hash.get(curr.stable_hash())
        if bucket:
            prev = bucket.pop()
            matched_prev.add(_identity(prev))
            _classify_match(prev, curr, diff)  # same element, id churned
        else:
            still_new.append(curr)

    # Whatever is left is genuinely new / removed.
    for curr in still_new:
        diff.changes.append(DomChange(ChangeKind.NEW, curr))
        diff.new_backend_ids.add(curr.backend_node_id)
    for prev in previous_nodes:
        if _identity(prev) not in matched_prev:
            diff.changes.append(DomChange(ChangeKind.REMOVED, prev))

    return diff


def _classify_match(prev: EnhancedDOMTreeNode, curr: EnhancedDOMTreeNode, diff: DomDiff) -> None:
    """A matched previous/current pair -> MOVED and/or MODIFIED (or nothing)."""

    if prev.parent_branch_hash() != curr.parent_branch_hash():
        diff.changes.append(DomChange(ChangeKind.MOVED, curr))
    if _state_signature(prev) != _state_signature(curr):
        diff.changes.append(
            DomChange(ChangeKind.MODIFIED, curr, _describe_modification(prev, curr))
        )


def render_change_block(diff: DomDiff) -> str:
    """The compact 'changes since last step' text prepended to the observation.
    Empty string when nothing changed (the caller omits the section)."""

    if not diff.has_changes:
        return ""

    lines: list[str] = ["## Changes since last step"]
    for kind, label in (
        (ChangeKind.NEW, "NEW"),
        (ChangeKind.MODIFIED, "MODIFIED"),
        (ChangeKind.MOVED, "MOVED"),
        (ChangeKind.REMOVED, "REMOVED"),
    ):
        for change in diff.of_kind(kind):
            node = change.node
            # Must match what the serializer rendered, or the change block would
            # hand the model a ref the ref index cannot resolve.
            index = (
                node.selector_index
                if node.selector_index is not None
                else node.backend_node_id
            )
            ref = f"e{index}"
            name = f' "{node.ax_name}"' if node.ax_name else ""
            role = node.ax_role or node.tag_name
            suffix = f" ({change.detail})" if change.detail else ""
            marker = f"[{ref}]" if kind is not ChangeKind.REMOVED else "[was]"
            lines.append(f"{label}: {marker}<{role}{name}>{suffix}")
    return "\n".join(lines)
