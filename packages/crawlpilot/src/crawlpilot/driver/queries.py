"""Reading one element: text, markup, value, attributes, geometry, state.

Ported from agent-browser's getter/assertion tools (`agent_browser_get_text`,
`get_html`, `get_value`, `get_attr`, `get_count`, `get_box`, `get_styles`,
`is_visible`, `is_enabled`, `is_checked` -- `actions.rs:5550-5655, 7207-7344`).

**Why these exist next to `find_elements`.** `find_elements` answers "what is on
this page that matches a selector", in bulk. These answer "what is the state of
*this* element", singly, and they are what turns a guess into a check: a model
that can read a field's value back can tell a fill that worked from one that was
rejected by a validator, without spending a whole observation to find out.

Every verb takes a `ref` **or** a CSS `selector`. A ref is the cheap path -- the
element is already in the node index and needs no query -- but a caller may not
have snapshotted, and CSS is already accepted by `find_elements` and
`search_page`'s `css_scope`, so refusing it here would be an inconsistency
rather than a safeguard.

The one-argument JS convention is `patchright_driver`'s: parameters arrive as a
single object rather than interpolated into source, because a selector may come
from a model and a quote or backslash in it would otherwise break out of the
script.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from crawlpilot.spi.errors import SelectorNotFound

if TYPE_CHECKING:
    from crawlpilot.spi.dom_tree import EnhancedDOMTreeNode

DEFAULT_STYLE_PROPERTIES: tuple[str, ...] = (
    "display",
    "visibility",
    "opacity",
    "color",
    "background-color",
    "font-size",
    "font-weight",
    "position",
    "z-index",
)
"""What `get_styles` reads when the caller names nothing.

A curated handful rather than the whole computed style, which is several hundred
properties per element -- enough to bury the answer and, on a page with many
elements, to cost more context than the observation it was meant to save.
"""

# `__read` is shared verbatim by the selector path and the ref path, so the two
# cannot drift about what, say, "visible" means.
_READ_PRELUDE = """
    function __read(el, opts) {
        const style = getComputedStyle(el);
        const rect = el.getBoundingClientRect();
        const styles = {};
        for (const name of (opts.properties || [])) styles[name] = style.getPropertyValue(name);
        return {
            text: (el.innerText ?? el.textContent ?? '').trim(),
            html: el.innerHTML,
            outer: el.outerHTML,
            value: 'value' in el ? String(el.value ?? '') : null,
            attr: opts.attribute ? el.getAttribute(opts.attribute) : null,
            checked: 'checked' in el ? Boolean(el.checked)
                     : (el.getAttribute('aria-checked') === 'true'),
            indeterminate: 'indeterminate' in el ? Boolean(el.indeterminate) : false,
            // Disabled is inheritable through <fieldset>, so ask the DOM rather
            // than reading the attribute off this element alone.
            enabled: !(el.disabled === true || el.closest('[disabled]') !== null
                       || el.getAttribute('aria-disabled') === 'true'),
            // "Visible" the way a person means it: laid out, not display:none,
            // not visibility:hidden, not fully transparent, and with real size.
            visible: !!(rect.width || rect.height || el.getClientRects().length)
                     && style.display !== 'none'
                     && style.visibility !== 'hidden'
                     && parseFloat(style.opacity || '1') > 0,
            box: {
                x: rect.x + window.scrollX,
                y: rect.y + window.scrollY,
                width: rect.width,
                height: rect.height,
            },
            styles: styles,
        };
    }
"""


SELECTOR_READ_JS = f"""(opts) => {{
{_READ_PRELUDE}
    const el = document.querySelector(opts.selector);
    return el ? __read(el, opts) : null;
}}"""
"""Selector path: query, then read. `null` means nothing matched."""

NODE_READ_JS = f"""function(opts) {{
{_READ_PRELUDE}
    return __read(this, opts);
}}"""
"""Ref path, as a `Runtime.callFunctionOn` body -- `this` is the element the
node index already resolved, so no query happens at all."""


def options_for(
    *,
    attribute: str | None = None,
    properties: tuple[str, ...] | list[str] | None = None,
    selector: str | None = None,
) -> dict[str, Any]:
    return {
        "selector": selector,
        "attribute": attribute,
        "properties": list(properties) if properties else [],
    }


def describe_box(box: dict[str, Any]) -> str:
    return (
        f"x={box['x']:.0f} y={box['y']:.0f} "
        f"width={box['width']:.0f} height={box['height']:.0f}"
    )


def missing(selector: str) -> SelectorNotFound:
    return SelectorNotFound(selector)


def target_description(ref: str | None, selector: str | None) -> str:
    """How to name the element in a readout, so a caller reading a batch of them
    can tell which answer belongs to which question."""

    if ref is not None:
        return ref
    return repr(selector)


def require_target(ref: str | None, selector: str | None) -> None:
    if ref is None and selector is None:
        raise ValueError("one of `ref` or `selector` is required")


def node_object_ref(node: EnhancedDOMTreeNode) -> dict[str, Any]:
    return {"backendNodeId": node.backend_node_id}
