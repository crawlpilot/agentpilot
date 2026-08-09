"""Per-site block / HTML-integrity checkers -- a port of Pulsar's
`ChainedHtmlIntegrityChecker` + its site implementations
(`AmazonHtmlIntegrityChecker.kt`, `JdHtmlIntegrityChecker.kt`, and the in-crawler
`WalmartHtmlChecker` in `walmart/WalmartCrawler.kt`).

Each checker keys off the *final* URL (after any silent redirect) and the page
source. Two signals the generic classifier can't see:

  1. **Redirect-to-block-page tells** -- Akamai/retail WAFs often 200-redirect a
     bot to a `/blocked` or login/verify URL rather than serving a 403, so the
     status is clean and only the landed URL betrays the wall.
  2. **Per-page-type minimum content size** (`requireSize`) -- a real product
     page is hundreds of KB; a stub/wall that returns HTTP 200 is tiny. Below the
     site's floor the page is `TOO_SMALL` (a CRAWL-scope soft retry).

Checkers return `None` to defer (no opinion) so the chain falls through to the
generic markers in `block_detect.classify_page`. Install the defaults once via
`install_default_site_checkers()` (block_detect does this at import).
"""

from __future__ import annotations

import os

from agentpilot.extraction.block_detect import Verdict, register_site_checker

# Amazon CAPTCHA prompt (AmazonHtmlIntegrityChecker.kt:120): a *short* page
# carrying this exact prompt is a robot check.
_AMAZON_CAPTCHA = "type the characters you see in this image"
_AMAZON_ROBOT_MAX_LEN = 150_000

# requireSize floors (chars of page source). Ported from Pulsar's `-requireSize`
# load args / AmazonHtmlIntegrityChecker.SMALL_CONTENT_LIMIT.
_WALMART_ITEM_MIN = 300_000  # /ip/ product pages
_WALMART_PORTAL_MIN = 250_000  # browse/brands portal pages
_AMAZON_ITEM_MIN = 250_000  # /dp/ or /gp/product/ item pages (SMALL_CONTENT_LIMIT/2)
_AMAZON_GENERIC_MIN = 1_000  # other Amazon pages


class WalmartChecker:
    """Walmart (`walmart/WalmartCrawler.kt:27-42`): a landed `/blocked` or
    `/verify` URL is a definitive wall (ROBOT_CHECK_3, the heaviest severity);
    a `403 Forbidden` body is FORBIDDEN; an undersized product page is TOO_SMALL."""

    def is_relevant(self, url: str) -> bool:
        return "walmart.com" in url.lower()

    def check(self, *, html: str | None, url: str, status: int | None) -> Verdict | None:
        lurl = url.lower()
        if "/blocked" in lurl or "blocked?" in lurl or "/verify" in lurl:
            return Verdict.ROBOT_CHECK_3
        body = html or ""
        if "403 forbidden" in body.lower():
            return Verdict.FORBIDDEN
        if html is not None:
            floor = _WALMART_ITEM_MIN if "/ip/" in lurl else _WALMART_PORTAL_MIN
            if len(body) < floor:
                return Verdict.TOO_SMALL
        return None


class AmazonChecker:
    """Amazon (`AmazonHtmlIntegrityChecker.kt`): the CAPTCHA prompt on a short
    page is a robot check; a `/dp/` item page below the size floor is TOO_SMALL;
    an optional (env-gated) delivery-district mismatch is WRONG_GEO."""

    def is_relevant(self, url: str) -> bool:
        return "amazon." in url.lower()

    def check(self, *, html: str | None, url: str, status: int | None) -> Verdict | None:
        if html is None:
            return None
        body = html
        lower = body.lower()
        if len(body) < _AMAZON_ROBOT_MAX_LEN and _AMAZON_CAPTCHA in lower:
            return Verdict.ROBOT_CHECK
        # Delivery-district mismatch (wrong proxy geo) -- opt-in, since the
        # expected district depends on the proxy's exit country. When
        # AGENTPILOT_AMAZON_EXPECT_DISTRICT is set and the delivery block is
        # present but doesn't mention it, the egress geo is wrong (CRAWL retry).
        expect = os.environ.get("AGENTPILOT_AMAZON_EXPECT_DISTRICT", "").strip().lower()
        if expect and "glow-ingress-block" in lower and expect not in lower:
            return Verdict.WRONG_GEO
        lurl = url.lower()
        is_item = "/dp/" in lurl or "/gp/product/" in lurl
        floor = _AMAZON_ITEM_MIN if is_item else _AMAZON_GENERIC_MIN
        if len(body) < floor:
            return Verdict.TOO_SMALL
        return None


class JdChecker:
    """JD (`JdHtmlIntegrityChecker.kt:74-90`): an `item.jd.com` page redirected
    to a login URL is a robot check; a `403 Forbidden` body is FORBIDDEN."""

    def is_relevant(self, url: str) -> bool:
        return "jd.com" in url.lower()

    def check(self, *, html: str | None, url: str, status: int | None) -> Verdict | None:
        lurl = url.lower()
        if "login" in lurl:
            return Verdict.ROBOT_CHECK_3
        if html is not None and "403 forbidden" in html.lower():
            return Verdict.FORBIDDEN
        return None


def install_default_site_checkers() -> None:
    """Register the built-in Walmart/Amazon/JD checkers. Called once at import
    from `block_detect`; safe to call again only if the chain was cleared."""
    register_site_checker(WalmartChecker())
    register_site_checker(AmazonChecker())
    register_site_checker(JdChecker())
