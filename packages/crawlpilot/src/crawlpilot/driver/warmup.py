"""Pre-read human warm-up -- a port of Pulsar's `CommonRPA.visit` routine
(`exotic-app/.../rpa/CommonRPA.kt:87-135`) and its scroll profile
(`InteractSettings.kt:42-66,193-209`).

Akamai's `_abck` cookie only becomes valid after 2-3 accepted sensor POSTs,
which the sensor script emits in response to real pointer/scroll telemetry. A
straight Navigate -> Extract never produces any, so the cookie stays in its
`~-1~` not-yet-solved state and the next request is walled. This module does
what a human idly does on arrival -- a handful of small wheel scrolls with
human gaps and a dwell -- then waits for `_abck` to flip valid before the
caller reads content.

Two faithful choices from the Kotlin source:
  - **wheel, not JS scroll.** Pulsar's realistic scroll uses CDP
    `Input.dispatchMouseEvent` (`PulsarWebDriver.kt:512-536`) so the events are
    *trusted* and fire the page's real scroll listeners; a `window.scrollBy`
    would not feed the sensor. Patchright's `mouse.wheel` is the CDP-backed
    equivalent.
  - **the exact counts/deltas** from `CommonRPA.visit`: `2 + rand(0,4)` scrolls
    of `100 + 20*rand(0,9)` px (100-280) with a 0.5-1.0 s gap each.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import time
from collections.abc import Mapping, Sequence
from typing import Any

import structlog
from patchright.async_api import Page

from crawlpilot.driver import mouse
from crawlpilot.driver.humanize import DelayPolicy
from crawlpilot.extraction import block_detect

log = structlog.get_logger(__name__)

# InteractSettings.kt:42-66 -- viewport-height fractions to rest the scroll at.
_INIT_SCROLL_FRACTIONS = (0.3, 0.75)

_ABCK_POLL_MAX_S = 30.0
"""Upper bound on the _abck wait. The site either validates us inside this or
isn't going to, and we let the caller proceed/handle the block rather than hang
the scrape.

Was 8s, which is shorter than a pass takes. MEASURED on cos.com, native Chrome,
residential IP: `_abck` flipped to `~0~` at 6.0s, 6.0s and 8.6s, and one run
reached the real document only at 25s, through a challenge stub. An emulated
worker is 3-4x slower at the sensor's JS than that. Non-Akamai pages never pay
it -- `wait_for_abck` short-circuits once it sees no Akamai cookie."""

_CHALLENGE_SETTLE_MAX_S = 20.0
"""Upper bound on waiting for Akamai's sensor stub to reload into the real page,
after `_abck` validates (see `settle_akamai_challenge`)."""

# Cookies Akamai Bot Manager sets. If none of these is present the site isn't
# Akamai-protected, so the _abck wait short-circuits rather than burning the
# full timeout on every stealth scrape of a non-Akamai page.
_AKAMAI_COOKIES = frozenset({"_abck", "ak_bmsc", "bm_sz", "bm_sv", "bm_mi"})


def _is_akamai(cookies: Sequence[Mapping[str, Any]]) -> bool:
    return any(c.get("name") in _AKAMAI_COOKIES for c in cookies)


def _wheel_scroll_count() -> int:
    # CommonRPA.kt:  n = 2 + Random.nextInt(5)  -> 2..6 scrolls.
    return 2 + random.randint(0, 4)


def _wheel_delta_px() -> float:
    # CommonRPA.kt:  deltaY = 100.0 + 20 * Random.nextInt(10)  -> 100..280 px.
    return 100.0 + 20.0 * random.randint(0, 9)


_DRIFT_MOVES = (2, 4)
"""How many separate pointer drifts the warm-up makes. Akamai's sensor scores
the *stream* of `mousemove` coordinates (its `bmak.mme` counters weigh pointer
velocity and acceleration heavily), and a session that scrolls but never moves
the pointer produces a sensor POST with an empty movement channel -- which is
its own signal, regardless of whether `_abck` eventually validates."""


async def human_mouse_drift(page: Page, policy: DelayPolicy) -> None:
    """Move the pointer along a few curved paths across the viewport.

    This is the piece the original `CommonRPA.visit` port left out: it emits
    wheel events only. Best-effort like the rest of warm-up -- a detached page
    mid-drift is swallowed, never raised into the scrape.
    """

    try:
        size = page.viewport_size or {"width": 1280, "height": 800}
    except Exception:
        return

    width, height = float(size["width"]), float(size["height"])
    # Start wherever a pointer plausibly is on arrival: somewhere in the upper
    # half, not pinned at the origin.
    current = (random.uniform(0.2, 0.8) * width, random.uniform(0.1, 0.4) * height)

    for _ in range(random.randint(*_DRIFT_MOVES)):
        target = (random.uniform(0.1, 0.9) * width, random.uniform(0.15, 0.85) * height)
        for x, y in mouse.path(current, target):
            try:
                await page.mouse.move(x, y)
            except Exception:
                return
            # A few ms between samples: this is intra-gesture motion, not an
            # inter-action pause, so it must not use the action delay table.
            await asyncio.sleep(random.uniform(0.008, 0.022))
        current = target
        await policy.pause("gap")


async def human_scroll(page: Page, policy: DelayPolicy) -> None:
    """A short burst of trusted wheel scrolls with human gaps -- the core of
    the warm-up, and reusable on its own for pages that just need engagement
    telemetry. Best-effort: a wheel failure (detached page mid-scroll) is
    swallowed so warm-up never fails the scrape it precedes."""

    for _ in range(_wheel_scroll_count()):
        try:
            await page.mouse.wheel(0, _wheel_delta_px())
        except Exception:
            return
        await policy.pause("mouseWheel")


def _abck_value(cookies: Sequence[Mapping[str, Any]]) -> str | None:
    for c in cookies:
        if c.get("name") == "_abck":
            return c.get("value")
    return None


async def wait_for_abck(
    page: Page, policy: DelayPolicy, *, timeout_s: float = _ABCK_POLL_MAX_S
) -> bool:
    """Poll the context cookies until `_abck` looks validated, nudging the page
    with a small scroll between polls to keep sensor POSTs flowing. Returns
    whether a valid cookie was seen; a `False` is advisory (the caller decides
    whether to proceed and let block-detection handle a wall), not fatal."""

    deadline = time.monotonic() + timeout_s
    nudged = False
    while time.monotonic() < deadline:
        try:
            cookies = await page.context.cookies()
        except Exception:
            return False
        if block_detect.is_abck_valid(_abck_value(cookies)):
            return True
        if nudged and not _is_akamai(cookies):
            # After at least one nudge, still no Akamai cookies -> not an Akamai
            # site; don't burn the timeout waiting for a sensor cookie that will
            # never appear. (The `nudged` grace avoids racing the first response,
            # which may set the cookies a beat after navigation.)
            return False
        # A single nudge to elicit the next sensor submission, then wait.
        try:
            await page.mouse.wheel(0, _wheel_delta_px())
        except Exception:
            return False
        nudged = True
        await policy.pause("gap")
    return False


async def settle_akamai_challenge(
    page: Page, *, timeout_s: float = _CHALLENGE_SETTLE_MAX_S
) -> bool:
    """Wait while the page is Akamai's sensor-challenge stub. True once it is not.

    The stub (`block_detect.is_akamai_challenge`) reloads itself into the real
    page when its sensor POST is accepted, which can land *after* `_abck` has
    already validated -- so a read straight after the cookie wait can still get
    the stub. Polls the DOM rather than waiting on a navigation event, because the
    reload may already have happened by the time this runs. A read that throws
    means a navigation is in flight, which is exactly what we are waiting for.
    """

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            html = await page.content()
        except Exception:
            await asyncio.sleep(0.25)
            continue
        if not block_detect.is_akamai_challenge(html):
            with contextlib.suppress(Exception):
                await page.wait_for_load_state("domcontentloaded", timeout=5_000)
            return True
        await asyncio.sleep(0.5)
    log.info("warmup.akamai_challenge_unsettled", url=page.url)
    return False


async def warm_up(page: Page, policy: DelayPolicy, *, wait_abck: bool = False) -> bool:
    """Run the pre-read warm-up on an already-navigated page: settle on `body`,
    scroll like a human, and -- when `wait_abck` (Akamai targets) -- wait for a
    valid `_abck`. Returns the `_abck` outcome (`True` when not waiting). Never
    raises: warm-up is a best-effort enhancement, not a gate."""

    try:
        await page.wait_for_selector("body", timeout=int(policy.sample("waitForSelector") * 10))
    except Exception:
        pass
    await human_mouse_drift(page, policy)
    await human_scroll(page, policy)
    if not wait_abck:
        return True
    ok = await wait_for_abck(page, policy)
    if not ok:
        log.info("warmup.abck_unvalidated", url=page.url)
        return ok
    await settle_akamai_challenge(page)
    return ok
