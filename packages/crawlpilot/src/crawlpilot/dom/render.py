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

# What a selector can actually be written against, added on top of
# `DEFAULT_INCLUDE_ATTRIBUTES` when `SnapshotView.for_authoring` is set.
# `class` is excluded from the default set (browser-use comments it out) because
# it is noise to an agent deciding what to click -- and it is the single most
# useful thing to an agent deciding what to select.
AUTHORING_INCLUDE_ATTRIBUTES: tuple[str, ...] = DEFAULT_INCLUDE_ATTRIBUTES + (
    "class",
    "itemprop",
    "data-qa-qualifier",
)

# Attributes that make an element nameable. An element carrying none of them
# cannot be selected except by position, so rendering it would cost a line and
# buy the model nothing.
_ADDRESSABLE = ("id", "class", "itemprop", "data-testid", "data-qa-qualifier")


def _authoring_line(node: SimplifiedNode, include_attributes: tuple[str, ...]) -> str | None:
    """A structural line for a non-interactive element that holds text.

    Only elements that DIRECTLY contain text: a wrapper whose text all lives
    three levels down is not where a selector should point, and rendering every
    ancestor would bury the page in scaffolding.
    """

    original = node.original
    if original.node_type != NodeType.ELEMENT_NODE:
        return None
    if not any(original.attributes.get(a) for a in _ADDRESSABLE):
        return None
    holds_text = any(
        child.original.node_type == NodeType.TEXT_NODE and child.text_content()
        for child in node.children
    )
    if not holds_text:
        return None
    return f"<{original.tag_name}{_attribute_string(node, include_attributes)}>"

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


# How far into a control to look for the image that describes it, and how many
# to report. A gallery thumbnail wraps its `<img>` two or three levels down; a
# product card that contains a dozen is describing a listing, not itself.
_MAX_IMAGE_DESCENDANTS = 60
_MAX_IMAGE_CONTEXTS = 2


def _child_image_context(node: SimplifiedNode) -> str:
    """`image_alt=...` for the image a nameless control is built around.

    Ported from browser-use's `_get_child_image_context`. The case is an image
    gallery: every thumbnail is a button whose only human-readable description
    is the `alt` of the `<img>` inside it. That `<img>` is not interactive and
    has no children, so `_build_simplified` prunes it outright -- and the
    attribute whitelist can only report `alt` on the element carrying it. The
    description therefore reaches the model nowhere at all, and the control
    renders as an anonymous `[eN]<button />`.

    Measured on a Zara product page: eight gallery controls named "Side view of
    a multicoloured bag with an asymmetric top" in the accessibility tree,
    every one of them anonymous in the render.

    Walks the ORIGINAL descendants for exactly that reason -- the simplified
    tree no longer contains the image this is looking for.
    """

    parts: list[str] = []
    seen = 0
    stack = [node.original]
    while stack and seen < _MAX_IMAGE_DESCENDANTS and len(parts) < _MAX_IMAGE_CONTEXTS:
        current = stack.pop()
        seen += 1
        if current.tag_name == "img":
            for attribute, label in (("alt", "image_alt"), ("title", "image_title")):
                value = normalize_text(current.attributes.get(attribute, ""))
                if value:
                    if len(value) > _MAX_VALUE_LEN:
                        value = value[:_MAX_VALUE_LEN] + "\u2026"
                    parts.append(f"{label}={value}")
                    break
        stack.extend(reversed(list(current.children_and_shadow_roots)))
    return "".join(f" {part}" for part in parts)


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
    if not ax_name:
        # An image button's only description lives on the `<img>` inside it,
        # and that `<img>` is not itself a rendered line -- so without this the
        # control shows as an anonymous `[e12]<button />`. Containment dedup
        # then makes it worse: when a wrapper and its inner image box are both
        # interactive, the inner one is dropped and the survivor is the one
        # with the generic label ("Enlarge image") rather than the descriptive
        # alt ("Side view of a multicoloured bag").
        #
        # Only when the element has no accessible name of its own: a control
        # that already says what it is does not need its decoration described.
        attrs += _child_image_context(node)
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
        # Two different things keep an element out of a render, and the marker
        # has to be honest about which one this is or it sends the model after
        # the wrong remedy:
        #
        # - THIS cut is by document order against a character budget. Scrolling
        #   does not move it, so "scroll to see the rest" is wrong here.
        # - Separately, `is_visible` is viewport-gated (`dom_fusion_engine`
        #   `_VIEWPORT_THRESHOLD_PX`), so an off-screen control earns no ref at
        #   all and scrolling genuinely does fix THAT. The offscreen-controls
        #   trailer in `serializer.py` is what reports it.
        #
        # So this says only what it knows: the list was cut here.
        marker = (
            "\n… [truncated: the element list was cut short here to fit. "
            "Anything below is missing from this list only] …"
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
