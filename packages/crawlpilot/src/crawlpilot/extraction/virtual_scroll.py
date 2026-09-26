"""Virtual-scroll accumulation: recovering the whole feed from a container that
recycles its DOM.

A virtualized list keeps only the visible rows in the document. Scroll down and
the rows that left the viewport are *removed* and their nodes reused for the rows
arriving at the bottom. Twitter, Instagram, most infinite product grids and every
`react-window` table work this way. So `page.content()` on such a page returns one
viewport of rows no matter how far down the page you are -- and a scraper that
scrolls and then extracts gets the *last* screen rather than all of them.

The fix is to extract after every scroll and merge. Which is where the actual
difficulty is, and it is not the scrolling:

**Consecutive snapshots overlap, by an unknown amount.** A scroll of 80% of the
container height leaves 20% of the previous rows still present. Concatenating
snapshots duplicates them; deduplicating by exact row text loses genuinely
repeated rows (two identical "Load more" entries, two products with the same
name). So the merge has to find the *overlap* between the tail of what is kept and
the head of what arrived, and splice — which is a sequence-alignment problem, not a
set-union one.

This module is that merge, as a pure function over a list of snapshots. Keeping it
out of the driver is deliberate: the CDP half is a loop that needs a real browser
to exercise, and the merge is where every off-by-one lives. Here it is testable
with a list of strings.

Adapted from crawl4ai's `VirtualScrollConfig` handling (Apache-2.0; crawl4ai
0.9.4, `crawl4ai/async_configs.py` and the virtual-scroll branch of its crawler).
The config shape is theirs. The merge is not: upstream collects the recycled HTML
and joins it, deduplicating whole snapshots when they are byte-identical, which
handles "the container stopped changing" and not the ordinary partial-overlap case
that every real scroll produces.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

ScrollBy = Literal["container_height", "page_height"] | int


@dataclass(frozen=True)
class VirtualScrollConfig:
    """How to drive a recycling container.

    Field names and defaults follow crawl4ai's `VirtualScrollConfig` so anyone
    moving between the two does not have to relearn them.
    """

    container_selector: str
    """CSS selector for the *scrollable* element -- the one with `overflow: auto`,
    which is often not the element holding the rows. Getting this wrong produces a
    single snapshot and no error, because scrolling a non-scrollable element
    silently does nothing."""
    scroll_count: int = 10
    """Maximum scrolls. A bound, not a target: `capture` stops early when the
    container stops producing new content, so a short feed costs a short run."""
    scroll_by: ScrollBy = "container_height"
    """`"container_height"` (a containerful at a time), `"page_height"` (a
    viewportful), or a pixel count."""
    wait_after_scroll_ms: int = 500
    """How long to let the framework render the incoming rows. Too short and the
    snapshot catches the container mid-recycle, with the old rows gone and the new
    ones not yet painted -- which looks exactly like the end of the feed."""
    max_snapshots: int = 200
    """Hard bound on retained snapshots, independent of `scroll_count`, since this
    runs against untrusted pages and each snapshot is a container's worth of HTML."""


def merge_snapshots(snapshots: list[str], *, min_overlap_lines: int = 1) -> str:
    """Splice overlapping snapshots of a recycling container into one document.

    Each snapshot is the container's text or markdown at one scroll position.
    Consecutive snapshots overlap by however much the scroll left on screen, so
    each is joined to the accumulated result at the longest suffix/prefix match.

    Repeated rows survive. That is the property a set-union approach loses, and it
    matters: a feed legitimately contains two posts with identical text, and a
    product grid contains the same price many times over. Only the *overlapping
    region* between adjacent snapshots is dropped, never a line that merely
    appeared before.
    """

    kept: list[str] = []
    for snapshot in snapshots:
        lines = [line for line in (raw.strip() for raw in snapshot.splitlines()) if line]
        if not lines:
            continue
        if not kept:
            kept = lines
            continue
        overlap = _overlap_length(kept, lines, min_overlap_lines=min_overlap_lines)
        kept.extend(lines[overlap:])
    return "\n".join(kept)


def _overlap_length(kept: list[str], incoming: list[str], *, min_overlap_lines: int) -> int:
    """How many of `incoming`'s leading lines already sit at the end of `kept`.

    Longest match first, so a container whose snapshots overlap heavily splices at
    the true seam rather than at the first coincidental one-line match. Bounded by
    both lengths, so a snapshot entirely contained in what is already kept
    contributes nothing rather than being appended twice.
    """

    limit = min(len(kept), len(incoming))
    for length in range(limit, min_overlap_lines - 1, -1):
        if kept[-length:] == incoming[:length]:
            return length
    return 0


def _js_string(value: str) -> str:
    """`value` as a JavaScript string literal.

    `json.dumps`, not Python's `!r`: JSON string syntax is a subset of
    JavaScript's, so the result is valid by definition. Python's repr is valid for
    most strings and is not a guarantee -- and the selector here is
    caller-supplied, so "valid for most strings" is the wrong standard.
    """

    return json.dumps(value)


def scroll_delta_expression(config: VirtualScrollConfig) -> str:
    """The JavaScript expression for one scroll step's pixel distance.

    Evaluated against the container element rather than computed in Python because
    `container_height` is only knowable in the page, and re-reading it each step is
    what makes this work on a container that grows as content loads.
    """

    if isinstance(config.scroll_by, int):
        return str(config.scroll_by)
    if config.scroll_by == "page_height":
        return "window.innerHeight"
    return "el.clientHeight"


def build_scroll_script(config: VirtualScrollConfig) -> str:
    """One scroll step, returning whether the container actually moved.

    The return value is the termination signal: a container already at its bottom
    reports no movement, and `capture` stops rather than spending the remaining
    `scroll_count` on a feed that ended. Comparing `scrollTop` before and after is
    more reliable than comparing content, since a feed whose next page is still
    loading legitimately has unchanged content and a changed position.
    """

    return f"""
    (() => {{
      const el = document.querySelector({_js_string(config.container_selector)});
      if (!el) return {{ found: false, moved: false, scrollTop: 0 }};
      const before = el.scrollTop;
      el.scrollTop = before + ({scroll_delta_expression(config)});
      return {{ found: true, moved: el.scrollTop > before, scrollTop: el.scrollTop }};
    }})()
    """


def build_container_html_script(config: VirtualScrollConfig) -> str:
    """The container's current inner HTML, or `null` when the selector misses."""

    return f"""
    (() => {{
      const el = document.querySelector({_js_string(config.container_selector)});
      return el ? el.innerHTML : null;
    }})()
    """
