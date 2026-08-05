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
    previous_ids = _collect_interactive_identities(previous.root) if previous is not None else set()
    lines: list[str] = []
    _render_node(current.root, depth=0, path=(), previous_ids=previous_ids, lines=lines)
    return "\n".join(lines) if lines else "(empty page)"


def _node_identity(path: tuple[str, ...], node: SnapshotNode) -> str:
    """A structural identity: the `role:name` path from the root to this node.

    Stable across snapshots as long as the element keeps its role, name, and
    ancestry -- unlike the ephemeral `ref`, which Playwright re-mints by
    traversal order every snapshot, so a single insertion shifts every later
    ref and would spuriously mark unrelated elements "new". Identical siblings
    collapse to one identity, which is acceptable: the `*` prefix is a hint,
    not an addressing token (the `ref` remains the addressing token)."""

    return "\x1f".join((*path, f"{node.role}:{node.name}"))


def _collect_interactive_identities(node: SnapshotNode, path: tuple[str, ...] = ()) -> set[str]:
    ids: set[str] = set()
    is_synthetic_root = node.role == "root" and not node.ref
    if not is_synthetic_root and node.ref and node.role in INTERACTIVE_ROLES:
        ids.add(_node_identity(path, node))
    child_path = path if is_synthetic_root else (*path, f"{node.role}:{node.name}")
    for child in node.children:
        ids |= _collect_interactive_identities(child, child_path)
    return ids


def _render_node(
    node: SnapshotNode,
    *,
    depth: int,
    path: tuple[str, ...],
    previous_ids: set[str],
    lines: list[str],
) -> None:
    is_synthetic_root = node.role == "root" and not node.ref
    if not is_synthetic_root:
        text = _format_node(node, path, previous_ids)
        if text:
            lines.append(f"{'  ' * depth}{text}")
    next_depth = depth if is_synthetic_root else depth + 1
    child_path = path if is_synthetic_root else (*path, f"{node.role}:{node.name}")
    for child in node.children:
        _render_node(
            child, depth=next_depth, path=child_path, previous_ids=previous_ids, lines=lines
        )


def _format_node(node: SnapshotNode, path: tuple[str, ...], previous_ids: set[str]) -> str:
    name_part = f' "{node.name}"' if node.name else ""

    if node.ref and node.role in INTERACTIVE_ROLES:
        is_new = bool(previous_ids) and _node_identity(path, node) not in previous_ids
        prefix = "*" if is_new else ""
        bbox_part = f" @({int(node.bbox.x)},{int(node.bbox.y)})" if node.bbox else ""
        return f"{prefix}[{node.ref}]<{node.role}{name_part}{bbox_part}/>"

    if node.name:
        return f"{node.role}{name_part}"

    return ""
