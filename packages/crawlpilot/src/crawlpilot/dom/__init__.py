"""LLM-facing DOM pipeline: interactivity detection, simplification/occlusion
filtering, and serialization of the fused CDP tree into the compact form the
agent model reads. Built on top of the enriched node in `crawlpilot.spi.dom_tree`.

`serializer` (tree -> indexed text) and `diff` (what changed between two
captures) are the two published modules -- the entry point and the change
detector. `clickable_elements`, `paint_order` and `render` are stages *inside*
the pipeline: they exist to be composed by `serializer`, their signatures follow
from how it composes them, and pinning them as public would freeze the pipeline's
internal shape.
"""

from __future__ import annotations

from crawlpilot.dom import diff, serializer

__all__ = ["diff", "serializer"]
