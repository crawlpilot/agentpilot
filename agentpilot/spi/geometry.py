"""Driver-agnostic geometry primitives shared across the fusion pipeline.

`BoundingBox` lives here (rather than in a perception-engine module) so both the
fused DOM tree (`spi.dom_tree`), the serializer (`dom.serializer`), and the CDP
snapshot fusion (`driver.dom_fusion*`) can depend on it without coupling to any
particular perception engine.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class BoundingBox:
    x: float
    y: float
    width: float
    height: float
