"""Renders an `AXSnapshot` into the indented text form the agent loop's LLM
prompt actually reads -- `[ref]<role "name">` for actionable elements, plain
`role "name"` for informational context, `*[` prefix for a ref that wasn't
present in the previous step's snapshot (mirrors browser-use's own
new-element marker convention). Pure, stateless: takes both snapshots as
arguments rather than tracking "the previous one" itself.

Deliberately not a from-scratch DOM+CDP-layout+accessibility fusion pipeline
(browser-use's `DomService`/`DOMTreeSerializer`) -- this renders the
accessibility tree crawlpilot already builds (`spi.snapshot.AXSnapshot`),
classifying "interactive" by a fixed ARIA-role allowlist rather than
recomputing visibility/interactivity from raw layout.
"""

from __future__ import annotations

from agentpilot.spi.snapshot import AXSnapshot, SnapshotNode

INTERACTIVE_ROLES = frozenset(
    {
        "button",
        "link",
        "textbox",
        "searchbox",
        "checkbox",
        "radio",
        "combobox",
        "listbox",
        "slider",
        "spinbutton",
        "switch",
        "menuitem",
        "menuitemcheckbox",
        "menuitemradio",
        "tab",
        "option",
    }
)


def render_snapshot_for_llm(current: AXSnapshot, previous: AXSnapshot | None = None) -> str:
    previous_refs = _collect_refs(previous.root) if previous is not None else set()
    lines: list[str] = []
    _render_node(current.root, depth=0, previous_refs=previous_refs, lines=lines)
    return "\n".join(lines) if lines else "(empty page)"


def _collect_refs(node: SnapshotNode) -> set[str]:
    refs = {node.ref} if node.ref else set()
    for child in node.children:
        refs |= _collect_refs(child)
    return refs


def _render_node(
    node: SnapshotNode, *, depth: int, previous_refs: set[str], lines: list[str]
) -> None:
    is_synthetic_root = node.role == "root" and not node.ref
    if not is_synthetic_root:
        text = _format_node(node, previous_refs)
        if text:
            lines.append(f"{'  ' * depth}{text}")
    next_depth = depth if is_synthetic_root else depth + 1
    for child in node.children:
        _render_node(child, depth=next_depth, previous_refs=previous_refs, lines=lines)


def _format_node(node: SnapshotNode, previous_refs: set[str]) -> str:
    name_part = f' "{node.name}"' if node.name else ""

    if node.ref and node.role in INTERACTIVE_ROLES:
        is_new = bool(previous_refs) and node.ref not in previous_refs
        prefix = "*" if is_new else ""
        bbox_part = f" @({int(node.bbox.x)},{int(node.bbox.y)})" if node.bbox else ""
        return f"{prefix}[{node.ref}]<{node.role}{name_part}{bbox_part}/>"

    if node.name:
        return f"{node.role}{name_part}"

    return ""
