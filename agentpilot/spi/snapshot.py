"""Driver-agnostic ref-annotated accessibility snapshot tree.

Refs are epoch-scoped: every node carries the epoch it was born in, minted
per `SnapshotAction` call. The ref->locator cascade (P1) degrades gracefully
within an epoch and hard-fails across epochs -- see `spi.errors.StaleRefError`.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SnapshotNode:
    epoch: int
    ref: str
    role: str
    name: str
    children: list[SnapshotNode] = field(default_factory=list)


@dataclass
class AXSnapshot:
    epoch: int
    root: SnapshotNode
