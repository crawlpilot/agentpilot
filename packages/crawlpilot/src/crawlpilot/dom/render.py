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

_ZERO_WIDTH = str.maketrans(
    "",
    "",
    "﻿"  # BOM / zero-width no-break space
    "​"  # zero-width space
    "‌"  # zero-width non-joiner
    "‍"  # zero-width joiner
    "⁠",  # word joiner
)
"""Characters that occupy no width, so deleting them changes nothing a reader
sees. Sites emit them as CSS-free line-break hints and as tracking/scraper bait,
and they make two visually identical names compare unequal -- which is how the
same button reads as "new" on every step."""

_UNICODE_SPACES = str.maketrans(
    {
        c: " "
        for c in "          "
        "     　"
    }
)
"""Spaces that are *not* deleted, only folded.

agent-browser's `INVISIBLE_CHARS` (`snapshot.rs:68-75`) lumps `U+00A0` in with
the zero-width set and removes it, which turns a non-breaking-space-separated
`Add to cart` into `Addtocart` -- a name the model then cannot match against
anything it reads on the page. A non-breaking space *is* visible: it is a space.
Folding it to `U+0020` gets the intended benefit (identical-looking names
compare equal) without corrupting the text."""


def normalize_text(value: str) -> str:
    """The canonical form of any string shown to the model.

    Deletes zero-width characters, folds exotic Unicode spaces to `U+0020`, then
    collapses runs of whitespace. A string that was *only* invisible characters
    normalizes to `""`, which is what makes callers treat it as absent rather
    than as a mysterious unnamed node.
    """

    return " ".join(value.translate(_ZERO_WIDTH).translate(_UNICODE_SPACES).split())


def _attribute_string(node: SimplifiedNode, include_attributes: tuple[str, ...]) -> str:
    original = node.original
    attrs = original.attributes
    parts: list[str] = []
    is_password = attrs.get("type") == "password"
    for key in include_attributes:
        if key not in attrs:
            continue
        value = normalize_text(attrs[key])
        # Never leak a password field's value to the model.
        if is_password and key == "value":
            value = _REDACTED
        # Drop an attribute value that merely repeats the accessible name.
        # Both sides are normalized, so a name and an `aria-label` that differ
        # only by a stray zero-width character are still recognised as the same
        # string and the duplicate is dropped.
        elif value == normalize_text(original.ax_name) and key not in ("id", "type", "name"):
            continue
        if not value:
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
    # A promoted role wins over the node's own: this wrapper *is* the radio as
    # far as the user (and so the model) is concerned, and its real role is the
    # uninformative `LabelText` Chrome left behind. See
    # `serializer._promoted_control`.
    role = node.promoted.role if node.promoted else (original.ax_role or original.tag_name)
    ax_name = normalize_text(original.ax_name)
    name = f' "{ax_name}"' if ax_name else ""
    attrs = _attribute_string(node, include_attributes)
    if node.promoted is not None and node.promoted.checked is not None:
        attrs += f" checked={node.promoted.checked}"
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
    depth: int | None = None,
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

    def walk(node: SimplifiedNode, level: int) -> None:
        # `depth` caps how deep a *rendered* line may sit; the walk still
        # descends, because indentation counts only lines that were emitted, so
        # a control can sit at level 1 with ten structural wrappers above it.
        # Cutting the traversal instead would drop it for being nested, not deep.
        too_deep = depth is not None and level > depth
        indent = "\t" * level
        child_level = level
        if node.selector_index is not None:
            if not too_deep:
                lines.append(f"{indent}{_element_line(node, include_attributes)}")
                line_indices.append(node.selector_index)
            child_level = level + 1
        else:
            # `excluded_by_view` on a text node is `visible_text_only` saying a
            # person could not read this -- collapsed, painted over, or a single
            # stray glyph. The node stays in the tree (it is still an ancestor
            # and still contributes structure); it just contributes no line.
            text = "" if node.excluded_by_view else node.text_content()
            if text:
                if not too_deep:
                    lines.append(f"{indent}{text}")
                    line_indices.append(None)
                child_level = level + 1
        for child in node.children:
            walk(child, child_level)

    walk(root, 0)
    body = "\n".join(lines)
    kept = len(lines)
    if max_length is not None and len(body) > max_length:
        # NOT "scroll to see more". Nothing here filters by scroll position --
        # the cut is by document order against a character budget -- so that
        # advice sent the model into an unwinnable loop: it scrolled, the same
        # prefix came back, and it scrolled again until the step budget ran out.
        # Observed on a Zara product page, where the four accordion buttons the
        # task needed sat past the cut and the model spent every step trying to
        # bring them "into view" to earn a ref.
        marker = (
            "\n… [truncated: more elements exist. Scrolling will NOT reveal them"
            " -- find them by text or role instead] …"
        )
        # The marker is part of the output, so on a very small budget it has to
        # give way rather than push the render past the cap its caller asked for.
        if len(marker) > max_length:
            marker = "\n… [truncated] …"
        budget = max(0, max_length - len(marker))
        # Clamped, so `max_length` means what it says even when the budget is
        # too small to hold the marker itself.
        body = (body[:budget] + marker)[:max_length]
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
