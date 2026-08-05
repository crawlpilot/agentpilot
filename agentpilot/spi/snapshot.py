"""Driver-agnostic ref-annotated accessibility snapshot tree.

Refs are epoch-scoped: every node carries the epoch it was born in, minted
per `SnapshotAction` call. The ref->locator cascade (P1) degrades gracefully
within an epoch and hard-fails across epochs -- see `spi.errors.StaleRefError`.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field


@dataclass
class BoundingBox:
    x: float
    y: float
    width: float
    height: float


@dataclass
class SnapshotNode:
    epoch: int
    ref: str
    role: str
    name: str
    children: list[SnapshotNode] = field(default_factory=list)
    bbox: BoundingBox | None = None
    """Populated only for leaf/interactive nodes, only after
    `agentpilot.agent`-driven snapshots ask for it -- see `patchright_driver
    .py`'s `_annotate_bounding_boxes`. `None` for a node whose box wasn't
    resolved (not requested, detached, or a transient lookup failure)."""


@dataclass
class AXSnapshot:
    epoch: int
    root: SnapshotNode

    def fingerprint(self) -> str:
        """A stable content hash of the tree's `(role, name)` structure plus
        node count -- deliberately *excludes* the ephemeral `ref` (re-minted
        every snapshot) and `bbox` (scroll jitter). Two snapshots of an
        unchanged page hash identically; any meaningful DOM change flips it.
        Used by the loop's stagnation detector (mirrors Browser4's
        `PageStateTracker.calculatePageStateHash` over the aria snapshot)."""

        h = hashlib.sha256()
        count = 0
        stack = [self.root]
        while stack:
            node = stack.pop()
            h.update(node.role.encode("utf-8", "replace"))
            h.update(b"\x1f")
            h.update(node.name.encode("utf-8", "replace"))
            h.update(b"\x1e")
            count += 1
            # Push children reversed so the walk is a stable left-to-right
            # pre-order (structure-sensitive, not just a bag of nodes).
            stack.extend(reversed(node.children))
        h.update(f"#{count}".encode())
        return h.hexdigest()[:16]
