"""Parses Playwright/Patchright's `aria_snapshot(mode="ai")` text format into
`spi.snapshot` types, plus the P1 "snapshot token budget" filters
(`plan.md`: "aria_snapshot on a real commerce page is enormous; agents choke
without it").

The format is indented (2 spaces/level) lines like:

    - generic [active] [ref=e1]:
      - heading "Hello" [level=1] [ref=e2]
      - link "A link" [ref=e3] [cursor=pointer]:
        - /url: "#"

Lines are `- role "name"? [attr]* :?`. Metadata lines (e.g. `/url:`) carry no
`ref=` attribute; they're kept as informational tree nodes with `ref=""` so
depth/nesting stays correct, but the ref->locator cascade (P1's
`driver/ref_cache.py`) only ever resolves nodes with a non-empty ref.
"""

from __future__ import annotations

import re
from collections import deque

from agentpilot.spi.snapshot import SnapshotNode

_LINE_RE = re.compile(r"^(?P<indent> *)- (?P<body>.*)$")
_HEAD_RE = re.compile(r'^(?P<role>\S+)(?:\s+"(?P<name>(?:[^"\\]|\\.)*)")?\s*(?P<rest>.*)$')
_ATTR_RE = re.compile(r"\[([^\]]*)\]")


def parse_aria_snapshot(text: str, epoch: int) -> SnapshotNode:
    root = SnapshotNode(epoch=epoch, ref="", role="root", name="")
    stack: list[tuple[int, SnapshotNode]] = [(-1, root)]

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue
        m = _LINE_RE.match(line)
        if not m:
            continue

        depth = len(m.group("indent")) // 2
        body = m.group("body").rstrip()
        if body.endswith(":"):
            body = body[:-1].rstrip()

        hm = _HEAD_RE.match(body)
        if not hm:
            continue

        ref = ""
        for attr in _ATTR_RE.findall(hm.group("rest")):
            if attr.startswith("ref="):
                ref = attr[len("ref=") :]

        node = SnapshotNode(
            epoch=epoch,
            ref=ref,
            role=hm.group("role"),
            name=hm.group("name") or "",
        )

        while stack and stack[-1][0] >= depth:
            stack.pop()
        stack[-1][1].children.append(node)
        stack.append((depth, node))

    return root


def _copy_shallow(node: SnapshotNode) -> SnapshotNode:
    return SnapshotNode(epoch=node.epoch, ref=node.ref, role=node.role, name=node.name)


def filter_snapshot(
    root: SnapshotNode, *, roles: tuple[str, ...] | None, max_nodes: int | None
) -> SnapshotNode:
    """Pure, tree-shape-only filtering -- no page access needed, unlike
    `viewport_only` (see `patchright_driver.py`), so it's unit-testable
    against static trees.

    `roles`: prunes any subtree whose root role isn't in `roles`, *unless* it
    has a matching descendant -- structural *container* nodes (no `ref` but
    with children, e.g. the synthetic `role="root"`) are always kept as
    scaffolding around whatever they contain, since dropping them would
    silently reparent unrelated matches. Ref-less *leaf* nodes (e.g. a
    link's `/url: "#"` metadata line, which also carries no `ref`) are NOT
    given this passthrough -- they're informational annotations, not
    containers, so they're dropped like any other non-matching leaf; giving
    them a free pass would keep every filtered-out link/image alive purely
    because it happens to have one of these metadata children.
    `max_nodes`: breadth-first budget so a truncated tree still reads as a
    coherent (if incomplete) page rather than one deep sliver.
    """

    def keep(node: SnapshotNode) -> SnapshotNode | None:
        children = [c for c in (keep(child) for child in node.children) if c is not None]
        is_structural_container = not node.ref and bool(node.children)
        matches = roles is None or node.role in roles or is_structural_container
        if not matches and not children:
            return None
        copy = _copy_shallow(node)
        copy.children = children
        return copy

    filtered = keep(root) or _copy_shallow(root)
    if max_nodes is not None:
        filtered = _truncate_snapshot(filtered, max_nodes)
    return filtered


def _truncate_snapshot(root: SnapshotNode, max_nodes: int) -> SnapshotNode:
    """Breadth-first: keeps the root, then fills the node budget level by
    level (rather than depth-first, which would keep one whole deep branch
    and drop entire unrelated sections)."""

    budget = max(max_nodes - 1, 0)
    new_root = _copy_shallow(root)
    queue: deque[tuple[SnapshotNode, SnapshotNode]] = deque([(root, new_root)])
    while queue and budget > 0:
        orig, copy = queue.popleft()
        for child in orig.children:
            if budget <= 0:
                break
            child_copy = _copy_shallow(child)
            copy.children.append(child_copy)
            budget -= 1
            queue.append((child, child_copy))
    return new_root


def collect_leaf_refs(root: SnapshotNode) -> list[str]:
    """Refs belonging to nodes with no children -- the candidates
    `viewport_only` resolves bounding boxes for. Container nodes are skipped
    since their own box roughly spans their children's anyway, halving the
    number of `bounding_box()` round trips without changing which content is
    judged visible."""

    refs: list[str] = []

    def walk(node: SnapshotNode) -> None:
        if not node.children and node.ref:
            refs.append(node.ref)
        for child in node.children:
            walk(child)

    walk(root)
    return refs


def prune_to_refs(root: SnapshotNode, visible: set[str]) -> SnapshotNode:
    """Keeps only nodes whose ref is in `visible`, plus structural
    (ref-less) ancestors of any kept node -- the counterpart filter to
    `collect_leaf_refs`, applied after `viewport_only` bounding-box checks."""

    def keep(node: SnapshotNode) -> SnapshotNode | None:
        children = [c for c in (keep(child) for child in node.children) if c is not None]
        if node.children:
            if not children:
                return None  # container whose children were all pruned
        elif node.ref and node.ref not in visible:
            return None  # leaf ref judged off-screen
        copy = _copy_shallow(node)
        copy.children = children
        return copy

    return keep(root) or _copy_shallow(root)
