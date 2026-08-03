"""Driver-agnostic ref-annotated accessibility snapshot tree.

Refs are epoch-scoped: every node carries the epoch it was born in, minted
per `SnapshotAction` call. The ref->locator cascade (P1) degrades gracefully
within an epoch and hard-fails across epochs -- see `spi.errors.StaleRefError`.
"""

from __future__ import annotations

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
