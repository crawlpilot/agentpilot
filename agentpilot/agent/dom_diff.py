"""Cross-step DOM change identification -- the headline of the fusion port.

Instead of re-sending the whole page and making the model re-derive what changed
(a `*`-only, `role:name`-path marking of new elements), `diff_snapshots`
compares the interactive elements of two fused trees and produces a typed change
report: **NEW / REMOVED / MOVED / MODIFIED**. Rendered as a compact "changes
since last step" block, this is both the clearest signal for the agent and the
biggest per-step token saving (a delta instead of a full re-read).

Identity model (mirrors browser-use's `(session_id, backend_node_id)`
set-difference for NEW, generalized): elements are matched across steps by the
CDP `backend_node_id` -- stable within a document -- with `stable_hash`
(structure + static attrs + accessible name, dynamic CSS classes filtered) as a
fallback that absorbs the backend-id reassignment a re-render can cause. Only
after both keys fail to match is an element considered genuinely NEW/REMOVED.

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

from agentpilot.dom.clickable_elements import is_interactive
from agentpilot.spi.dom_tree import EnhancedDOMTreeNode

_STATE_PROPS = ("checked", "expanded", "pressed", "selected", "disabled")


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
        vt = node.ax_node.properties.get("valuetext")
        if isinstance(vt, str) and vt:
            return vt
    return node.attributes.get("value", "")


def _state_signature(node: EnhancedDOMTreeNode) -> tuple:
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
    prev_by_backend = {n.backend_node_id: n for n in previous_nodes}
    cur_by_backend = {n.backend_node_id: n for n in current_nodes}

    matched_prev: set[int] = set()  # backend ids consumed by a match
    matched_cur: set[int] = set()

    # Tier 1: match by stable backend_node_id.
    for backend_id, curr in cur_by_backend.items():
        prev = prev_by_backend.get(backend_id)
        if prev is None:
            continue
        matched_prev.add(backend_id)
        matched_cur.add(backend_id)
        _classify_match(prev, curr, diff)

    # Tier 2: absorb backend-id reassignment -- match leftovers by stable_hash.
    prev_by_hash: dict[int, list[EnhancedDOMTreeNode]] = {}
    for n in previous_nodes:
        if n.backend_node_id not in matched_prev:
            prev_by_hash.setdefault(n.stable_hash(), []).append(n)

    still_new: list[EnhancedDOMTreeNode] = []
    for curr in current_nodes:
        if curr.backend_node_id in matched_cur:
            continue
        bucket = prev_by_hash.get(curr.stable_hash())
        if bucket:
            prev = bucket.pop()
            matched_prev.add(prev.backend_node_id)
            _classify_match(prev, curr, diff)  # same element, id churned
        else:
            still_new.append(curr)

    # Whatever is left is genuinely new / removed.
    for curr in still_new:
        diff.changes.append(DomChange(ChangeKind.NEW, curr))
        diff.new_backend_ids.add(curr.backend_node_id)
    for prev in previous_nodes:
        if prev.backend_node_id not in matched_prev:
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
            ref = f"e{node.backend_node_id}"
            name = f' "{node.ax_name}"' if node.ax_name else ""
            role = node.ax_role or node.tag_name
            suffix = f" ({change.detail})" if change.detail else ""
            marker = f"[{ref}]" if kind is not ChangeKind.REMOVED else "[was]"
            lines.append(f"{label}: {marker}<{role}{name}>{suffix}")
    return "\n".join(lines)
