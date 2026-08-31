"""Waiting for the page to reach a state.

Ported from agent-browser's wait tools, which share one `poll_until_true` helper
(`actions.rs:5735-5922`) rather than each rolling its own loop -- the same shape
kept here, for the same reason: the timeout, the poll interval and the
"what do we do when it expires" decision are identical for every condition, and
having them in one place is what stops one of them silently differing.

**A wait that expires raises.** The tempting alternative -- return a boolean and
let the caller decide -- is what makes waits worthless in a batch: the action
after the wait runs against the state the caller was waiting *not* to see, and
reports success. `WaitTimeout` is a `DriverError` so it surfaces the same way a
navigation timeout does.

Polling rather than CDP events, deliberately. The conditions are page-level
predicates (a selector matches, text is present, the URL changed), and Chrome has
no event for most of them; a poll is what Playwright does under its own
`wait_for_selector` too. The interval is small enough to be imperceptible and
large enough not to busy-loop a renderer that is trying to finish loading.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from fnmatch import fnmatch

from crawlpilot.spi.errors import WaitTimeout

POLL_INTERVAL_SECONDS = 0.1


async def poll_until(
    predicate: Callable[[], Awaitable[bool]],
    *,
    timeout_ms: int,
    description: str,
    poll_ms: int | None = None,
) -> None:
    """Call `predicate` until it returns True, or raise `WaitTimeout`.

    The predicate is always evaluated **once before any sleeping**: the condition
    is very often already true (the element was there all along), and a wait that
    slept first would add its poll interval to every such call for nothing.

    A predicate that raises is treated as "not yet, keep waiting" rather than as
    a failure. Mid-navigation the page can be in a state where evaluating
    anything throws, and that is exactly when a caller is most likely to be
    waiting -- failing there would turn a transient into an error.
    """

    interval = (poll_ms / 1000) if poll_ms is not None else POLL_INTERVAL_SECONDS
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        try:
            if await predicate():
                return
        except Exception:  # noqa: BLE001 -- see docstring
            pass
        if time.monotonic() >= deadline:
            raise WaitTimeout(description, timeout_ms)
        await asyncio.sleep(min(interval, max(0.0, deadline - time.monotonic())))


SELECTOR_STATE_JS = """(opts) => {
    const el = document.querySelector(opts.selector);
    if (opts.state === 'detached') return el === null;
    if (el === null) return false;
    if (opts.state === 'attached') return true;
    const style = getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    const visible = !!(rect.width || rect.height || el.getClientRects().length)
        && style.display !== 'none'
        && style.visibility !== 'hidden'
        && parseFloat(style.opacity || '1') > 0;
    return opts.state === 'hidden' ? !visible : visible;
}"""
"""`visible` is defined the same way `driver.queries` defines it -- laid out, not
`display:none`, not `visibility:hidden`, not fully transparent. Two definitions
of visible in one driver would mean `wait_for_selector(state="visible")` could
succeed on an element `is_visible` then calls hidden."""

TEXT_PRESENT_JS = """(opts) => {
    const body = document.body;
    if (!body) return false;
    return (body.innerText || body.textContent || '').includes(opts.text);
}"""
"""`innerText` first: it is what a reader sees, so it excludes `display:none`
subtrees that `textContent` would happily match. `textContent` is the fallback
for a document whose body has not been laid out yet."""


def url_matches(pattern: str, url: str) -> bool:
    """Substring, or glob when the pattern has wildcards.

    Two behaviours in one argument because a caller almost always means the
    first (`wait_for_url("/checkout")`) and reaches for the second only
    deliberately (`wait_for_url("https://*.example.com/**")`). Requiring a flag
    to distinguish them would make the common case carry an argument it never
    varies. agent-browser's `wait_for_url` takes the same "URL glob or pattern".
    """

    if any(ch in pattern for ch in "*?["):
        return fnmatch(url, pattern)
    return pattern in url
