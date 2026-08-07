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

import random

import structlog
from patchright.async_api import Page

from agentpilot.driver import block_detect
from agentpilot.driver.humanize import DelayPolicy

log = structlog.get_logger(__name__)

# InteractSettings.kt:42-66 -- viewport-height fractions to rest the scroll at.
_INIT_SCROLL_FRACTIONS = (0.3, 0.75)

_ABCK_POLL_MAX_S = 8.0
"""Upper bound on the _abck wait. Two-three accepted sensor POSTs typically
land within a few seconds of scrolling; beyond this the site either doesn't use
Akamai or isn't going to validate us, and we let the caller proceed/handle the
block rather than hang the scrape."""


def _wheel_scroll_count() -> int:
    # CommonRPA.kt:  n = 2 + Random.nextInt(5)  -> 2..6 scrolls.
    return 2 + random.randint(0, 4)


def _wheel_delta_px() -> float:
    # CommonRPA.kt:  deltaY = 100.0 + 20 * Random.nextInt(10)  -> 100..280 px.
    return 100.0 + 20.0 * random.randint(0, 9)


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


def _abck_value(cookies: list[dict]) -> str | None:
    for c in cookies:
        if c.get("name") == "_abck":
            return c.get("value")
    return None


async def wait_for_abck(page: Page, policy: DelayPolicy, *, timeout_s: float = _ABCK_POLL_MAX_S) -> bool:
    """Poll the context cookies until `_abck` looks validated, nudging the page
    with a small scroll between polls to keep sensor POSTs flowing. Returns
    whether a valid cookie was seen; a `False` is advisory (the caller decides
    whether to proceed and let block-detection handle a wall), not fatal."""

    import time

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            cookies = await page.context.cookies()
        except Exception:
            return False
        if block_detect.is_abck_valid(_abck_value(cookies)):
            return True
        # A single nudge to elicit the next sensor submission, then wait.
        try:
            await page.mouse.wheel(0, _wheel_delta_px())
        except Exception:
            return False
        await policy.pause("gap")
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
    await human_scroll(page, policy)
    if not wait_abck:
        return True
    ok = await wait_for_abck(page, policy)
    if not ok:
        log.info("warmup.abck_unvalidated", url=page.url)
    return ok
