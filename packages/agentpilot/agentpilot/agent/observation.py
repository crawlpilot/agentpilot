"""Assemble the per-step observation the agent model reads from a fused DOM tree
-- the integration point between the change-diff (`agent.dom_diff`) and the
serializer (`dom.serializer`). This is the sole perception path: a fused
`EnhancedDOMTreeNode` rendered to the model's indexed-element text.

Delta-first (checklist L): the compact "changes since last step" block leads the
observation so the model reads a *delta* rather than re-deriving changes from the
full tree; the serialized tree follows with inline `*` markers on new elements.
On the first step / after navigation there is no previous tree, so only the full
tree is rendered.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from agentpilot.agent.dom_diff import DomDiff, diff_snapshots, iter_interactive, render_change_block
from agentpilot.dom.serializer import serialize
from agentpilot.spi.dom_tree import DOMSelectorMap, EnhancedDOMTreeNode


@dataclass
class Observation:
    text: str
    """The full observation text: change block (if any) + serialized tree."""
    selector_map: DOMSelectorMap
    """Ref (`backendNodeId`) -> node, for ref-resolution of the model's actions."""
    diff: DomDiff
    """The structured change report, retained for stagnation/loop detection."""


def build_observation(
    current: EnhancedDOMTreeNode,
    previous: EnhancedDOMTreeNode | None = None,
    *,
    max_length: int | None = None,
) -> Observation:
    """Diff against the previous tree, serialize the current tree with the new
    elements marked, and prepend the change block."""

    diff = diff_snapshots(previous, current)
    serialized = serialize(current, new_backend_ids=diff.new_backend_ids, max_length=max_length)

    change_block = render_change_block(diff)
    if change_block:
        text = f"{change_block}\n\n{serialized.llm_text}"
    else:
        text = serialized.llm_text

    return Observation(text=text, selector_map=serialized.selector_map, diff=diff)


def identity_fingerprint(tree: EnhancedDOMTreeNode) -> str:
    """A stagnation fingerprint over the *stable identities* of the interactive
    elements (not the raw text), so trivial text churn doesn't reset the loop
    detector but a real interactive-set change does."""

    hashes = sorted(node.stable_hash() for node in iter_interactive(tree))
    digest = hashlib.sha256()
    for h in hashes:
        digest.update(h.to_bytes(8, "big", signed=False))
        digest.update(b"\x1e")
    digest.update(f"#{len(hashes)}".encode())
    return digest.hexdigest()[:16]
