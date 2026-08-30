"""Act on a captured fused node over CDP, by `(session_id, backend_node_id)`.

This is the resolution half of the browser-use port. The perception half already
hands us a node with a stable CDP identity; these helpers act on *that*, rather
than trying to describe the element well enough for a selector engine to find it
again. browser-use's action path derives no CSS or XPath at all
(`browser/watchdogs/default_action_watchdog.py`) and neither does this one.

Everything here is **Runtime-free**. `DOM`, `DOMSnapshot`, `Accessibility`,
`Page` and `Input` are all non-Runtime domains, so the whole interaction path
works under the UI-driven stealth tier (`no_runtime`), where a CDP `Runtime` call
is itself the leak. That is a deliberate divergence from browser-use, which
reaches for `Runtime.callFunctionOn` freely -- for occlusion checks, field
clearing and JS `.click()` fallbacks -- because it has no stealth posture to
keep. Where a verb genuinely cannot be done without JS (`<select>`), the caller
gates it rather than this module smuggling Runtime in.

Coordinates come from the node's **own** session. A cross-origin iframe is a
separate renderer reporting coordinates relative to itself, and its input events
must be dispatched into it, so there is no offset arithmetic here -- the frame of
reference is self-consistent as long as geometry and input use one session
(browser-use makes the same choice, `default_action_watchdog.py:762-764`).
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from typing import TYPE_CHECKING, Any

from crawlpilot.driver import mouse
from crawlpilot.spi.errors import StaleRefError

if TYPE_CHECKING:
    from patchright.async_api import CDPSession

    from crawlpilot.spi.dom_tree import EnhancedDOMTreeNode

_SCROLL_SETTLE_S = 0.05
"""Let the scroll land before reading geometry. browser-use uses the same pause
after `DOM.scrollIntoViewIfNeeded` -- the box model is read from the renderer's
last committed layout, so measuring immediately can return the pre-scroll rect."""


def session_for_node(
    node: EnhancedDOMTreeNode,
    *,
    page_session: CDPSession,
    frame_sessions: dict[str, CDPSession],
) -> CDPSession:
    """The CDP session that owns `node`.

    A `backendNodeId` is only meaningful in the session that captured it, so this
    trusts the identity stamped on the node at capture time rather than searching
    every session for something that answers -- a search would silently find the
    wrong renderer's node of the same id. Ported from browser-use's
    `cdp_client_for_node` (`browser/session.py:3968-4024`), including its order:
    the capture's session, then the frame, then the page.
    """

    for key in (node.session_id, node.frame_id, node.target_id):
        if key is not None and key in frame_sessions:
            return frame_sessions[key]
    return page_session


async def scroll_into_view(cdp: CDPSession, node: EnhancedDOMTreeNode) -> None:
    """Best-effort scroll. A node that cannot be scrolled to (`display:none`, a
    detached subtree) is not itself a failure: the geometry read that follows is
    what decides whether the element is actionable, and it gives a better error."""

    with contextlib.suppress(Exception):
        await cdp.send("DOM.scrollIntoViewIfNeeded", {"backendNodeId": node.backend_node_id})
        await asyncio.sleep(_SCROLL_SETTLE_S)


async def element_box(cdp: CDPSession, node: EnhancedDOMTreeNode, ref: str) -> mouse.Box:
    """The element's on-screen box in `cdp`'s coordinate space, scrolled into view.

    Two tiers, as in browser-use: `DOM.getContentQuads` first because it returns
    every fragment of an inline element that wraps across lines -- taking the
    largest gives a point genuinely inside the element, where a single union box
    can have its centre fall in the gap between two lines. `DOM.getBoxModel` is
    the fallback.

    Raises `StaleRefError` when neither reports geometry, which is what "the node
    is gone, or has no box at all" looks like from here.
    """

    await scroll_into_view(cdp, node)

    box = await _quad_box(cdp, node)
    if box is None:
        box = await _model_box(cdp, node)
    if box is None or box["width"] <= 0 or box["height"] <= 0:
        raise StaleRefError(ref, epoch_superseded=False)
    return box


async def _quad_box(cdp: CDPSession, node: EnhancedDOMTreeNode) -> mouse.Box | None:
    try:
        result = await cdp.send("DOM.getContentQuads", {"backendNodeId": node.backend_node_id})
    except Exception:
        return None
    boxes = [_box_from_quad(quad) for quad in result.get("quads") or []]
    boxes = [b for b in boxes if b["width"] > 0 and b["height"] > 0]
    if not boxes:
        return None
    return max(boxes, key=lambda b: b["width"] * b["height"])


async def _model_box(cdp: CDPSession, node: EnhancedDOMTreeNode) -> mouse.Box | None:
    try:
        result = await cdp.send("DOM.getBoxModel", {"backendNodeId": node.backend_node_id})
    except Exception:
        return None
    content = (result.get("model") or {}).get("content")
    if not content or len(content) < 8:
        return None
    return _box_from_quad(content)


def _box_from_quad(quad: list[float]) -> mouse.Box:
    """A CDP quad is 8 numbers: four `(x, y)` corners, clockwise from top-left.
    Rotated or skewed elements give a non-axis-aligned quad, so take the extent."""

    xs = quad[0::2]
    ys = quad[1::2]
    x, y = min(xs), min(ys)
    return {"x": x, "y": y, "width": max(xs) - x, "height": max(ys) - y}


async def viewport_size(cdp: CDPSession) -> tuple[float, float]:
    """`(width, height)` of this session's own viewport, for clamping a click
    point. Falls back to a common desktop size if layout metrics are
    unavailable -- a slightly wrong clamp beats failing the action."""

    try:
        metrics = await cdp.send("Page.getLayoutMetrics")
    except Exception:
        return (1280.0, 720.0)
    viewport = metrics.get("cssLayoutViewport") or metrics.get("layoutViewport") or {}
    return (
        float(viewport.get("clientWidth") or 1280),
        float(viewport.get("clientHeight") or 720),
    )


def clamp(point: tuple[float, float], width: float, height: float) -> tuple[float, float]:
    """Keep a click point inside the viewport. An element straddling the edge has
    a centre that can sit outside it, and Chrome silently drops an input event
    dispatched off-viewport."""

    x, y = point
    return (min(max(x, 1.0), width - 1.0), min(max(y, 1.0), height - 1.0))


# ------------------------------------------------------------------- input


async def dispatch_mouse(
    cdp: CDPSession,
    kind: str,
    x: float,
    y: float,
    *,
    button: str = "none",
    click_count: int = 0,
    buttons: int = 0,
) -> None:
    await cdp.send(
        "Input.dispatchMouseEvent",
        {
            "type": kind,
            "x": x,
            "y": y,
            "button": button,
            "clickCount": click_count,
            "buttons": buttons,
        },
    )


async def move_to(cdp: CDPSession, x: float, y: float) -> None:
    await dispatch_mouse(cdp, "mouseMoved", x, y)


async def click_at(cdp: CDPSession, x: float, y: float, *, click_count: int = 1) -> None:
    """Press and release at a point already moved to.

    `buttons=1` on the press is the bitmask of *held* buttons, which some
    frameworks read off the event to distinguish a real press from a synthesised
    one; it is 0 again on release because nothing is held by then.
    """

    await dispatch_mouse(
        cdp, "mousePressed", x, y, button="left", click_count=click_count, buttons=1
    )
    await dispatch_mouse(cdp, "mouseReleased", x, y, button="left", click_count=click_count)


async def wheel_at(cdp: CDPSession, x: float, y: float, dx: float, dy: float) -> None:
    """A wheel event over a point -- scrolls whatever container is under it.

    Used for scrolling an *element's* own overflow (a dropdown list, a
    virtualised table), where the point is what selects the container.
    """

    await cdp.send(
        "Input.dispatchMouseEvent",
        {"type": "mouseWheel", "x": x, "y": y, "deltaX": dx, "deltaY": dy},
    )


async def scroll_gesture(cdp: CDPSession, x: float, y: float, dx: float, dy: float) -> None:
    """Scroll the page by a synthesized gesture.

    Not `mouseWheel`: a single wheel event is delivered to the compositor and
    frequently lands with no effect at all in headless Chrome (the page reports
    `scrollY == 0` right after), which is the same race Browser4 documents as
    crbug.com/444929150. `Input.synthesizeScrollGesture` is Chrome's own
    scroll-driving primitive -- it runs the real scroll animation and does not
    return until the gesture completes, so the scroll has actually happened by
    the time the next action reads the position. browser-use makes the same split
    (`default_action_watchdog.py:2204-2255` page, `:2257-2345` element).

    `*Distance` is the distance the *content* travels, which is the opposite sign
    to a wheel delta: scrolling down (positive `dy`) moves content up.
    """

    await cdp.send(
        "Input.synthesizeScrollGesture",
        {
            "x": x,
            "y": y,
            "xDistance": -dx,
            "yDistance": -dy,
            # Fast enough not to add seconds to an agent step, slow enough that
            # the page's own scroll handlers (lazy loading, sticky headers) run.
            "speed": 3000,
        },
    )


# Keys that carry a name rather than a character, with the legacy
# `windowsVirtualKeyCode` Chrome still expects for correct `keyCode`/`which`
# values in page-side handlers.
_NAMED_KEYS: dict[str, tuple[str, int]] = {
    "Enter": ("Enter", 13),
    "Tab": ("Tab", 9),
    "Escape": ("Escape", 27),
    "Backspace": ("Backspace", 8),
    "Delete": ("Delete", 46),
    "ArrowUp": ("ArrowUp", 38),
    "ArrowDown": ("ArrowDown", 40),
    "ArrowLeft": ("ArrowLeft", 37),
    "ArrowRight": ("ArrowRight", 39),
    "Home": ("Home", 36),
    "End": ("End", 35),
    "PageUp": ("PageUp", 33),
    "PageDown": ("PageDown", 34),
    "Space": ("Space", 32),
    # Modifiers are dispatched as key events in their own right (see
    # `press_key`), so they need descriptors too. `code` is the physical key,
    # which is side-specific -- pages that distinguish left from right read it.
    "Control": ("ControlLeft", 17),
    "Shift": ("ShiftLeft", 16),
    "Alt": ("AltLeft", 18),
    "Meta": ("MetaLeft", 91),
}

_MODIFIER_BITS = {"Alt": 1, "Control": 2, "Meta": 4, "Shift": 8}

PRIMARY_MODIFIER = "Meta" if sys.platform == "darwin" else "Control"
"""The "do the thing" modifier for this host: Cmd on macOS, Ctrl elsewhere.

Chrome binds select-all, copy and friends to whichever the platform uses, and
the browser runs on the host, so `Control+a` is simply not select-all on a Mac.
Spelling a shortcut `ControlOrMeta+a` (Playwright's convention) resolves here."""

_MODIFIER_ALIASES = {
    "ctrl": "Control",
    "control": "Control",
    "cmd": "Meta",
    "command": "Meta",
    "meta": "Meta",
    "alt": "Alt",
    "option": "Alt",
    "shift": "Shift",
    "controlormeta": PRIMARY_MODIFIER,
    "commandorcontrol": PRIMARY_MODIFIER,
    "mod": PRIMARY_MODIFIER,
}


def parse_key(keys: str) -> tuple[list[str], str]:
    """Split `"Control+Shift+A"` into `(["Control", "Shift"], "A")`.

    Accepts the aliases models actually emit (`ctrl`, `cmd`, `option`), because a
    shortcut rejected on spelling is a step wasted for something the intent of
    which was never ambiguous.
    """

    parts = [p for p in keys.split("+") if p]
    if not parts:
        return ([], "")
    *raw_modifiers, key = parts
    modifiers = [_MODIFIER_ALIASES.get(m.lower(), m) for m in raw_modifiers]
    return ([m for m in modifiers if m in _MODIFIER_BITS], key)


def _key_descriptor(key: str) -> dict[str, Any]:
    """CDP's shape for one key: the `key` value a page sees, the physical `code`,
    and the legacy `windowsVirtualKeyCode` that `keyCode`/`which` are derived
    from -- still what a lot of shortcut handlers actually read."""

    if key in _NAMED_KEYS:
        code, vk = _NAMED_KEYS[key]
        # `key` is the name ("Control"); `code` is the physical key
        # ("ControlLeft"). They differ for modifiers and coincide for the rest.
        return {"key": key, "code": code, "windowsVirtualKeyCode": vk}
    if len(key) != 1:
        # An unrecognised named key (a rarely-used F-key, a vendor spelling).
        # Pass the name through rather than raising: Chrome ignores what it does
        # not know, and one unsupported key should not fail the whole action.
        return {"key": key, "code": key, "windowsVirtualKeyCode": 0}
    code = f"Key{key.upper()}" if key.isalpha() else f"Digit{key}" if key.isdigit() else ""
    return {"key": key, "code": code, "windowsVirtualKeyCode": ord(key.upper())}


async def press_key(cdp: CDPSession, keys: str) -> None:
    """Dispatch one key or shortcut to whatever currently has focus.

    Modifiers are pressed as real key events before the main key and released
    after, rather than only being set as a bitmask: a page listening for
    `keydown` on Control alone (a shortcut palette, a modal trap) sees nothing
    from a bitmask, and the sequence is what a keyboard actually produces.
    """

    modifiers, key = parse_key(keys)
    if not key:
        return
    mask = sum(_MODIFIER_BITS[m] for m in modifiers)

    for modifier in modifiers:
        await _dispatch_key(cdp, "rawKeyDown", _key_descriptor(modifier), _MODIFIER_BITS[modifier])

    descriptor = _key_descriptor(key)
    await _dispatch_key(cdp, "rawKeyDown", descriptor, mask)
    # A modified key is a command, not text: Control+A must not also type "a".
    if not mask and len(key) == 1:
        await _dispatch_key(cdp, "char", descriptor, mask, text=key)
    await _dispatch_key(cdp, "keyUp", descriptor, mask)

    for modifier in reversed(modifiers):
        await _dispatch_key(cdp, "keyUp", _key_descriptor(modifier), 0)


async def type_character(cdp: CDPSession, char: str) -> None:
    """One character as the browser would produce it: keyDown, char, keyUp.

    Deliberately not `Input.insertText`, which delivers the whole string with no
    key events at all -- a strong automation tell, and it skips the per-keystroke
    handlers that autocomplete and validation widgets hang off. browser-use never
    uses `insertText` either.
    """

    if char == "\n":
        await press_key(cdp, "Enter")
        return
    descriptor = _key_descriptor(char)
    # `keyDown` deliberately carries no `text`: Chrome inserts on *any* key event
    # that has one, so a `text` here and on the `char` below types every
    # character twice ("hello" arriving as "hheelllloo"). The `char` event is the
    # one that inserts; `keyDown`/`keyUp` are what page handlers listen for.
    await _dispatch_key(cdp, "keyDown", descriptor, 0)
    await _dispatch_key(cdp, "char", descriptor, 0, text=char)
    await _dispatch_key(cdp, "keyUp", descriptor, 0)


async def _dispatch_key(
    cdp: CDPSession,
    kind: str,
    descriptor: dict[str, Any],
    modifiers: int,
    *,
    text: str | None = None,
) -> None:
    params: dict[str, Any] = {"type": kind, "modifiers": modifiers, **descriptor}
    if text is not None:
        params["text"] = text
    await cdp.send("Input.dispatchKeyEvent", params)


# ------------------------------------------------------------------ fields


async def focus(cdp: CDPSession, node: EnhancedDOMTreeNode) -> bool:
    """`DOM.focus` on the node. Returns whether it took.

    The caller falls back to clicking the element, which is how a person focuses
    a field and works for the custom widgets (contenteditable shells, masked
    inputs) that `DOM.focus` refuses because the real focus target is a child.
    """

    try:
        await cdp.send("DOM.focus", {"backendNodeId": node.backend_node_id})
    except Exception:
        return False
    return True


async def read_value(cdp: CDPSession, node: EnhancedDOMTreeNode) -> str | None:
    """The field's current value, read from the accessibility tree.

    `Accessibility.getPartialAXTree` reports a textbox's value as its AX `value`,
    which makes this a Runtime-free read-back -- the point being that a fill can
    be *verified* rather than assumed to have worked, even under stealth. The
    previous implementation called Playwright's `locator.input_value()`, which
    needed a locator this design no longer has.
    """

    try:
        result = await cdp.send(
            "Accessibility.getPartialAXTree",
            {"backendNodeId": node.backend_node_id, "fetchRelatives": False},
        )
    except Exception:
        return None
    for ax_node in result.get("nodes") or []:
        if ax_node.get("backendDOMNodeId") != node.backend_node_id:
            continue
        value = ax_node.get("value") or {}
        if isinstance(value.get("value"), str):
            return value["value"]
    return None


async def set_file_input(cdp: CDPSession, node: EnhancedDOMTreeNode, paths: list[str]) -> None:
    """Attach files to an `<input type=file>` without opening a file chooser.

    One CDP call, no click: the native chooser is an OS dialog that no amount of
    page automation can drive, so intercepting it is not an option worth having.
    """

    await cdp.send(
        "DOM.setFileInputFiles", {"files": paths, "backendNodeId": node.backend_node_id}
    )


_SELECT_JS = """
function(values) {
    if (this.tagName !== 'SELECT') {
        return {error: 'element is a <' + this.tagName.toLowerCase() + '>, not a <select>'};
    }
    const wanted = new Set(values);
    const selected = [];
    for (const option of this.options) {
        const hit = wanted.has(option.value) || wanted.has(option.text.trim());
        option.selected = hit;
        if (hit) selected.push(option.value);
    }
    if (selected.length === 0) return {error: 'no option matched'};
    this.dispatchEvent(new Event('input', {bubbles: true}));
    this.dispatchEvent(new Event('change', {bubbles: true}));
    return {selected: selected};
}
"""


async def select_options(
    cdp: CDPSession, node: EnhancedDOMTreeNode, values: list[str]
) -> dict[str, Any]:
    """Select options in a `<select>`, matching on `value` or visible text.

    **The only Runtime call in the interaction path.** A native select popup is
    browser chrome rather than page content, so there is no coordinate an input
    event could target; browser-use reaches for `Runtime.callFunctionOn` here for
    the same reason. Callers running under the stealth tier should weigh that.

    The explicit `input` and `change` dispatch is what makes frameworks notice:
    assigning `selected` mutates the DOM without emitting either, so a React or
    Vue control would keep rendering its old value and the selection would appear
    to silently revert.
    """

    try:
        resolved = await cdp.send("DOM.resolveNode", {"backendNodeId": node.backend_node_id})
    except Exception:
        return {"error": "element is no longer on the page"}
    object_id = (resolved.get("object") or {}).get("objectId")
    if object_id is None:
        return {"error": "element is no longer on the page"}

    try:
        outcome = await cdp.send(
            "Runtime.callFunctionOn",
            {
                "objectId": object_id,
                "functionDeclaration": _SELECT_JS,
                "arguments": [{"value": values}],
                "returnByValue": True,
            },
        )
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        with contextlib.suppress(Exception):
            await cdp.send("Runtime.releaseObject", {"objectId": object_id})

    result = (outcome.get("result") or {}).get("value")
    return result if isinstance(result, dict) else {"error": "select returned no result"}


def dropdown_options(node: EnhancedDOMTreeNode) -> list[dict[str, str]]:
    """The `<option>`s of a select, read straight off the captured tree.

    Answered offline, with no CDP round trip at all: the fused capture already
    walked into the `<select>` and kept its children, so asking the browser again
    would be re-fetching something we hold. browser-use needs
    `Runtime.callFunctionOn` here only because its own tree does not retain them.
    """

    from crawlpilot.spi.dom_tree import iter_elements

    options: list[dict[str, str]] = []
    for element in iter_elements(node):
        if element.tag_name != "option":
            continue
        text = " ".join(element.all_text().split())
        options.append({"text": text, "value": element.attributes.get("value", text)})
    return options
