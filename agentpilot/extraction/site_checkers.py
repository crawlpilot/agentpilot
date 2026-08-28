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

from agentpilot.extraction.block_detect import (
    Verdict,
    has_known_wall_marker,
    register_site_checker,
)

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


# --- The three retailers agentpilot is actually blocked on. All Inditex/H&M
# storefronts render their PDPs client-side from a large JSON payload, so a real
# product page is hundreds of KB while a wall or a geo/consent stub is a few.
# The generic `_TOO_SMALL_LEN = 500` floor in `block_detect` is far too low to
# tell them apart -- a 2 KB DataDome interstitial sails through it as OK.
_FASHION_PDP_MIN = 120_000
"""Floor for a product page on these storefronts. Deliberately well under the
observed size of a real PDP: the cost of a false TOO_SMALL is one cheap
same-identity retry (CRAWL scope), while a false OK returns a wall to the
caller as if it were content."""

_FASHION_LISTING_MIN = 40_000
"""Category/listing pages are lighter than PDPs but still far from a stub."""

_FASHION_HOSTS = (
    "zara.com",
    "cosstores.com",
    "cos.com",
    "hm.com",
    "www2.hm.com",
)

# Landed-URL tells shared across the three. Inditex/H&M bounce a suspected bot
# to a consent/geo gate or an error route rather than serving a 403.
_FASHION_BLOCK_PATHS = ("/blocked", "/verify", "/errors/", "/error-page", "/challenge")


class FashionRetailChecker:
    """Zara / COS / H&M.

    These sit behind Akamai (Zara) and DataDome (COS, H&M). Two signals the
    generic classifier cannot see:

    1. **A landed URL that is not the one requested.** `classify_page` is given
       the browser's final location, so a silent 200-redirect to a consent gate
       or error route is visible here even though the status is clean.
    2. **A per-page-type size floor.** See `_FASHION_PDP_MIN`.
    """

    def is_relevant(self, url: str) -> bool:
        lurl = url.lower()
        return any(host in lurl for host in _FASHION_HOSTS)

    def check(self, *, html: str | None, url: str, status: int | None) -> Verdict | None:
        lurl = url.lower()
        if any(p in lurl for p in _FASHION_BLOCK_PATHS):
            return Verdict.ROBOT_CHECK_3
        if html is None:
            return None
        body = html.lower()
        # DataDome's interstitial is served on the requested URL with a 200 and
        # is small; the marker is what distinguishes it from a thin real page.
        if "captcha-delivery.com" in body or "geo.captcha-delivery" in body:
            return Verdict.ROBOT_CHECK_3
        # Defer on any page the generic classifier can name precisely. The
        # size floor below is a heuristic; `Access Denied` is not, and letting
        # the heuristic win would downgrade a hard FORBIDDEN to a soft retry.
        if has_known_wall_marker(html):
            return None
        floor = _FASHION_PDP_MIN if _looks_like_pdp(lurl) else _FASHION_LISTING_MIN
        if len(html) < floor:
            return Verdict.TOO_SMALL
        return None


def _looks_like_pdp(lurl: str) -> bool:
    """Product-detail URL shapes across the three storefronts: Zara/COS use a
    `-p01234567.html` suffix, H&M uses `/productpage.<article>.html`."""

    return (
        "/productpage." in lurl
        or ".html" in lurl
        and ("-p" in lurl.rsplit("/", 1)[-1] or "/product/" in lurl)
    )


def install_default_site_checkers() -> None:
    """Register the built-in Walmart/Amazon/JD/fashion-retail checkers. Called once at import
    from `block_detect`; safe to call again only if the chain was cleared."""
    register_site_checker(WalmartChecker())
    register_site_checker(AmazonChecker())
    register_site_checker(JdChecker())
    register_site_checker(FashionRetailChecker())
