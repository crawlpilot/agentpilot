"""Serialize a simplified interactive tree into the compact text the agent model
reads -- ported from browser-use's `SerializedDOMState.llm_representation` /
`DOMTreeSerializer.serialize_tree`.

Format (documented to the model in the system prompt):

    [e33]<button aria-label=Submit />   interactive element, ref = e<backendNodeId>
    *[e38]<button />                     NEW interactive element since last step
        plain text                       non-interactive text, indented under parent
    |IFRAME|<iframe />                   iframe boundary
    |SHADOW(open)|<div />                shadow host

LLM-optimization (checklist L): only a curated attribute whitelist is rendered,
password values are redacted, long values truncated, and the whole string is
capped to a token budget with an explicit truncation marker so the model never
mistakes a cut for the end of the page.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from crawlpilot.dom.serializer import SimplifiedNode


@dataclass(frozen=True)
class RenderedTree:
    """The text the model reads, plus the refs it can actually see in it."""

    text: str
    rendered_indices: set[int]
    """`selector_index`es that survived `max_length` truncation whole. A subset
    of the `selector_map` whenever the page was longer than the budget."""

# Curated attributes worth showing the model (browser-use DEFAULT_INCLUDE_ATTRIBUTES).
DEFAULT_INCLUDE_ATTRIBUTES: tuple[str, ...] = (
    "id",
    "type",
    "name",
    "role",
    "aria-label",
    "placeholder",
    "value",
    "alt",
    "title",
    "href",
    "aria-expanded",
    "aria-checked",
    "checked",
    "selected",
    "disabled",
    "data-testid",
)

_MAX_VALUE_LEN = 80
_REDACTED = "<redacted>"


def _attribute_string(node: SimplifiedNode, include_attributes: tuple[str, ...]) -> str:
    original = node.original
    attrs = original.attributes
    parts: list[str] = []
    is_password = attrs.get("type") == "password"
    for key in include_attributes:
        if key not in attrs:
            continue
        value = attrs[key]
        # Never leak a password field's value to the model.
        if is_password and key == "value":
            value = _REDACTED
        # Drop an attribute value that merely repeats the accessible name.
        elif value == original.ax_name and key not in ("id", "type", "name"):
            continue
        if len(value) > _MAX_VALUE_LEN:
            value = value[:_MAX_VALUE_LEN] + "…"
        parts.append(f"{key}={value}")
    return (" " + " ".join(parts)) if parts else ""


def _element_line(node: SimplifiedNode, include_attributes: tuple[str, ...]) -> str:
    original = node.original
    # `selector_index`, not `backend_node_id`: the two differ exactly when a
    # cross-origin frame's renderer reused an id, and the ref the model is shown
    # has to be the one the driver's index can look up.
    ref = f"e{node.selector_index}"
    role = original.ax_role or original.tag_name
    name = f' "{original.ax_name}"' if original.ax_name else ""
    attrs = _attribute_string(node, include_attributes)
    prefix = "*" if node.is_new else ""
    marker = ""
    if original.tag_name in ("iframe", "frame"):
        marker = "|IFRAME|"
    elif node.is_shadow_host and original.shadow_root_type:
        marker = f"|SHADOW({original.shadow_root_type})|"
    return f"{marker}{prefix}[{ref}]<{role}{name}{attrs} />"


def render_tree(
    root: SimplifiedNode,
    *,
    include_attributes: tuple[str, ...] = DEFAULT_INCLUDE_ATTRIBUTES,
    max_length: int | None = None,
) -> RenderedTree:
    """Render the simplified tree to indented text. Indexed (interactive) nodes
    render as `[ref]<...>`; kept non-interactive nodes contribute their text.
    `max_length` caps the output with a visible truncation marker.

    Returns the rendered indices as well as the text. The agent loop pre-validates
    the model's chosen refs against what it was *shown*, and truncation means the
    two differ: the `selector_map` covers the whole page while the model only ever
    saw the first `max_length` characters of it. Reporting the surviving indices
    lets the loop reject a ref the model could only have guessed, instead of
    waving it through because it happens to exist further down the page.
    """

    lines: list[str] = []
    line_indices: list[int | None] = []

    def walk(node: SimplifiedNode, depth: int) -> None:
        indent = "\t" * depth
        child_depth = depth
        if node.selector_index is not None:
            lines.append(f"{indent}{_element_line(node, include_attributes)}")
            line_indices.append(node.selector_index)
            child_depth = depth + 1
        else:
            text = node.text_content()
            if text:
                lines.append(f"{indent}{text}")
                line_indices.append(None)
                child_depth = depth + 1
        for child in node.children:
            walk(child, child_depth)

    walk(root, 0)
    body = "\n".join(lines)
    kept = len(lines)
    if max_length is not None and len(body) > max_length:
        marker = "\n… [truncated: more elements below, scroll or narrow the task] …"
        budget = max(0, max_length - len(marker))
        body = body[:budget] + marker
        # A line is "shown" only if it survived whole -- a ref cut mid-token is
        # not something the model can copy.
        kept, consumed = 0, 0
        for line in lines:
            consumed += len(line) + (1 if kept else 0)
            if consumed > budget:
                break
            kept += 1

    return RenderedTree(
        text=body,
        rendered_indices={index for index in line_indices[:kept] if index is not None},
    )
