"""Recipe-scoped page classification: is this a real page, or a wall?

This exists to close the most destructive failure in the whole pipeline. A
challenge page has no product name, no price and no measurements, which is
*indistinguishable* from "every selector broke" unless something explicitly
looks. Under v1 that meant a blocked replay reported every field failed,
`check_and_heal` read it as layout drift, re-explored against the wall, and
wrote a new recipe version derived from a CAPTCHA. `max_heal_attempts` bounds
the thrash; it does not prevent the corruption, and the version that gets
promoted is the broken one.

It is measurable, not hypothetical: a plain fetch of a Zara product URL returns
403 while the same URL in real Chrome returns the full page.

**Why here and not by re-enabling session-wide detection.**
`crawlpilot/session/interactive.py` sets `detect_blocks=False` for long-lived
sessions, and `tests/test_stealth_profile.py::
test_block_detection_is_opt_out_for_long_lived_sessions` is an explicit
regression guard: a scrape has an escalation ladder to answer
`ChallengeDetected` with, a session does not, and turning it on made every
agent-loop, recipe and session-lifecycle contract test fail on ordinary thin
pages. That default is right and stays. What a recipe needs is not an
exception mid-run -- it is a *verdict it can branch on*, which is what this
module returns.

The asymmetry is what makes it worth the extra page read: a blocked run
correctly identified costs one wasted fetch, and a blocked run mistaken for
drift costs the recipe.
"""

from __future__ import annotations

from dataclasses import dataclass

from crawlpilot.extraction.block_detect import Verdict, classify_page, is_blocked
from crawlpilot.session.interactive import InteractiveSession, execute_on_session
from crawlpilot.session.registry import RegistryProtocol
from crawlpilot.spi import actions as spi_actions
from crawlpilot.spi.driver import BrowserDriver

# Below this, a "page" is not a page. Kept well under the generic
# `_TOO_SMALL_LEN` in block_detect because a recipe target is a content page --
# a product or article -- not a redirect stub.
_MIN_PLAUSIBLE_HTML = 500


@dataclass(frozen=True)
class PageVerdict:
    verdict: Verdict
    blocked: bool
    landed_url: str
    html_len: int

    @property
    def reason(self) -> str:
        return f"page classified as {self.verdict.value} at {self.landed_url}"


async def classify_current_page(
    *,
    session: InteractiveSession,
    registry: RegistryProtocol,
    driver: BrowserDriver,
    requested_url: str = "",
) -> PageVerdict:
    """Classify whatever the session is currently showing.

    The *landed* URL is used, not the requested one: a WAF commonly
    200-redirects a bot to a block page without the fetcher ever seeing a
    redirect status, and only the landed URL betrays it.

    HTTP status and response headers are not available on the interactive
    session path, so this classifies on body markers plus the landed URL --
    which is precisely the signal set the 200-body walls (Akamai, Cloudflare,
    DataDome) require anyway. A status-based check would add little here and is
    already covered on the scrape path.
    """

    result = await execute_on_session(
        session,
        [spi_actions.GetUrlAction(), spi_actions.ExtractAction(format="html")],
        registry=registry,
        driver=driver,
    )
    landed = _first_readout(result) or requested_url
    html = result.extracts[0] if result.extracts else ""

    verdict = classify_page(html=html, url=landed, status=None)
    blocked = is_blocked(verdict)

    # A page that classified OK but carries almost no markup is not a page we
    # should let a heal learn from either. Treated as blocked-ish rather than
    # as "every field is missing", for the same reason as everything else here.
    if not blocked and len(html or "") < _MIN_PLAUSIBLE_HTML:
        return PageVerdict(Verdict.EMPTY, True, landed, len(html or ""))

    return PageVerdict(verdict, blocked, landed, len(html or ""))


def _first_readout(result: object) -> str:
    readouts = getattr(result, "readouts", None) or []
    for item in readouts:
        if isinstance(item, str) and item.strip():
            return item.strip()
    return ""
