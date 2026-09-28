"""Block / CAPTCHA / challenge classification -- a port of Pulsar's
`HtmlIntegrity` taxonomy and the per-site content markers its crawlers use to
tell "I was served a real page" from "I was served a bot wall".

Pulsar's status-code-only check is not enough for Akamai, which frequently
serves its **Access Denied** wall with an HTTP **200** and an
`errors.edgesuite.net` reference id in the body (exactly the failure that
prompted this work). So `classify_page` looks at status *and* body *and* URL,
mirroring:
  - `pulsar-common/.../Htmls.kt:20-112`             (the state taxonomy)
  - `exotic-server/.../AmazonHtmlIntegrityChecker.kt:96-125`  (robot-check length heuristic)
  - `exotic-app/.../walmart/WalmartCrawler.kt:33-41`          ("blocked" URL / "403 Forbidden" body)
  - `pulsar-protocol/.../util/HtmlIntegrityChecker.kt:52-86`  (empty/blank/no-anchor/too-small)

The `Verdict` a page earns drives two things downstream (Stage 4 escalation):
its `scope` (tear the whole identity+proxy down vs. retry the same identity)
and its `warning_weight` for burn accounting -- both ported from
`BrowserResponseHandlerImpl.kt:147-175` and `AbstractPrivacyContext.kt:330-347`.
"""

from __future__ import annotations

import enum
import re
from collections.abc import Mapping
from typing import Protocol

from crawlpilot.extensions.mounts import BlockHooks


class Verdict(enum.Enum):
    OK = "ok"
    # Graduated robot-check severities, ported from Pulsar's ROBOT_CHECK /
    # ROBOT_CHECK_2 / ROBOT_CHECK_3 (Htmls.kt:55-57). Higher severity carries a
    # heavier burn weight so a strong tell (a per-site checker's confident block,
    # e.g. Walmart's /blocked wall) retires an identity in fewer hits than an
    # ambiguous one. All three are PRIVACY-scope (full rotation).
    ROBOT_CHECK = "robot_check"  # CAPTCHA / JS challenge / Akamai sensor wall
    ROBOT_CHECK_2 = "robot_check_2"  # a stronger robot-check tell
    ROBOT_CHECK_3 = "robot_check_3"  # a definitive block wall (per-site checker)
    FORBIDDEN = "forbidden"  # hard 403 / Access Denied
    RATE_LIMITED = "rate_limited"  # 429
    WRONG_GEO = "wrong_geo"  # served, but wrong country/district/lang
    TOO_SMALL = "too_small"  # rendered, but suspiciously thin
    EMPTY = "empty"  # blank / no-body / no-anchor
    NOT_FOUND = "not_found"  # genuine 404, do not retry


_ROBOT_CHECKS = frozenset({Verdict.ROBOT_CHECK, Verdict.ROBOT_CHECK_2, Verdict.ROBOT_CHECK_3})


class Scope(enum.Enum):
    """What an unhealthy verdict should trigger (BrowserResponseHandlerImpl.kt).
    PRIVACY tears down the whole context (new browser + new proxy); CRAWL is a
    softer same-identity retry; NONE is terminal."""

    NONE = "none"
    CRAWL = "crawl"
    PRIVACY = "privacy"


# --- Akamai (the target). Access Denied is often a 200 with these markers.
_AKAMAI_MARKERS = (
    "access denied",
    "you don't have permission to access",
    "errors.edgesuite.net",
    "reference #",
)

# --- Akamai Bot Manager's sensor-challenge stub.
#
# Not a wall: a page that is *about to become* the real one. Captured verbatim
# from cos.com (2026-09-28) as the first document of a navigation, in two
# variants -- 1146 bytes, and 2733 bytes with a hidden behavioural challenge:
#
#     <div id="sec-if-cpt-container" role="main" style="display: none">
#     ...
#     var chlgeId = '';
#     ... if (pointer.responseURL.indexOf('t=' + chlgeId) > -1) location.reload(true);
#
# It proxies `XMLHttpRequest.send`, waits for the sensor POST carrying its
# challenge id, and reloads into the product page once Akamai accepts it. The
# 2733-byte variant has an `<a>` (Akamai's privacy link) and is over
# `_TOO_SMALL_LEN`, so before this it classified **OK** -- a read that raced the
# reload returned the stub as the scraped content. `warmup.settle_akamai_challenge`
# waits it out; a page still showing it afterwards did not pass.
_AKAMAI_CHALLENGE_MARKERS = (
    "sec-if-cpt",
    "chlgeid",
)


def is_akamai_challenge(html: str | None) -> bool:
    """Whether `html` is Akamai's in-flight sensor-challenge stub (see above)."""

    body = (html or "").lower()
    return any(m in body for m in _AKAMAI_CHALLENGE_MARKERS)


# --- Cloudflare / hCaptcha / Turnstile / generic interstitials.
_CHALLENGE_MARKERS = (
    "just a moment",
    "cf-browser-verification",
    "cf-challenge",
    "hcaptcha",
    "/cdn-cgi/challenge-platform",
    "attention required",
    # Turnstile is Cloudflare's current challenge widget and was missing
    # entirely -- `cf-challenge` above only covers the older interstitial.
    "cf-turnstile",
    "challenges.cloudflare.com",
)

# --- DataDome. Nothing in this module detected it before, which is why H&M's
# interstitials were returned to callers as if they were content. (COS was
# listed here too, but COS is Akamai -- `AkamaiGHost`, `_abck`, the `/akam/`
# pixel on every trace -- and its stub is `_AKAMAI_CHALLENGE_MARKERS` above.)
_DATADOME_MARKERS = (
    "geo.captcha-delivery.com",
    "interstitial.captcha",
)

# --- PerimeterX / HUMAN.
#
# The last two are the hold-the-button challenge's own heading and instruction,
# captured verbatim from a live walmart.com wall (2026-08-31):
#
#     <h2>Robot or human?</h2>
#     <p>Activate and hold the button to confirm that you're human.</p>
#     <div id="px-captcha" ...>
#
# Ungated, unlike the wordings in `_AMBIGUOUS_HOLD_MARKERS`, because they are
# challenge-specific rather than ordinary English -- and they have to be: that
# wall is 468 KB (it still ships Walmart's whole app shell), so a short-page
# gate would never fire on it. Verified absent from two full 1.9 MB product
# pages. Apostrophes are avoided deliberately -- "confirm that you're human" is
# a strong tell, but the quote character varies between straight and typographic.
_PERIMETERX_MARKERS = (
    "px-captcha",
    "please verify you are a human",
    "captcha.px-cdn.net",
    "robot or human",
    "activate and hold the button",
)

# --- Vendor *presence*, which is not the same thing as a block.
#
# These used to sit in the two tuples above and fire on any page containing
# them. That is wrong, and provably so: a served Walmart product page (1.9 MB,
# HTTP 200, real content) carries `*.perimeterx.net` in its CSP allowlist and
# `"perimeterX":{"enable":true,...}` in its own bootstrap JSON. So every
# successful Walmart PDP classified as `ROBOT_CHECK` -- a PRIVACY-scope verdict
# that raises `ChallengeDetected`, burns the identity and rotates the proxy.
# `run_ephemeral_scrape` runs with `detect_blocks=True`, so `scrape()` climbed
# the entire escalation ladder and then failed on pages it had *already
# fetched*.
#
# The module already reasons this way about headers -- "`x-datadome` is set on
# served *and* blocked responses ... only the value is a block" -- and about
# `set-cookie: datadome=`, called out there as "not a block by itself, just
# proof the site is DataDome-protected". A vendor's name in the body is the
# same class of evidence; it was the body path that never got the treatment.
#
# So they stay, but only count on a page too short to be real content -- the
# gate `AmazonChecker` already applies to its CAPTCHA prompt.
_VENDOR_PRESENCE_MARKERS = (
    "perimeterx",
    "_pxhd",
    "datadome",
    "dd_cookie",
    "captcha-delivery.com",
)

# --- The wording the same challenge uses on other PerimeterX deployments, and
# the name it is popularly known by.
#
# Size-gated, unlike the two prompts in `_PERIMETERX_MARKERS`, because this one
# is ordinary English: a real product page can easily carry "press and hold" in
# its own copy -- a power tool, an electric toothbrush, a blender. Ungated, the
# phrase would wall the exact catalogue it is meant to fetch.
_AMBIGUOUS_HOLD_MARKERS = (
    "press & hold",
    "press &amp; hold",  # the same prompt as it appears in HTML source
    "press and hold",
)

_VENDOR_PRESENCE_MAX_LEN = 150_000
"""Ceiling under which a vendor name or a challenge prompt counts as a block.

Same value and same reasoning as `AmazonChecker`'s `_AMAZON_ROBOT_MAX_LEN`: a
challenge page is a few KB, a real retail page hundreds of KB to megabytes (the
Walmart PDP above is 1.9 MB, and `WalmartChecker` already calls a `/ip/` page
under 300 KB undersized). Deliberately far above any real wall and far below any
real product page, so neither side is close to the line."""

# Response headers that identify a WAF outright, checked independently of the
# body: a challenge page can be visually indistinguishable from a thin real
# page, but these headers are unambiguous. `classify_page` only ever received
# `(html, url, status)` before, so all of this was invisible to it.
_DATADOME_HEADERS = ("x-datadome", "x-datadome-cid")
_PERIMETERX_HEADERS = ("x-px-block", "x-px-request-id")

# Amazon-style CAPTCHA and other site-specific tells now live in
# `agentpilot.control.retail_extension` (contributed through the extension
# of this module), not inline here.

# Below this, a "rendered" page is almost certainly a stub/wall, not content
# (generic HtmlIntegrityChecker "too small"; Amazon uses 500 KiB / 250 KiB for
# list/item pages -- this is the conservative generic floor).
_TOO_SMALL_LEN = 500

_ABCK_INVALID = re.compile(r"~-1~")
"""An Akamai `_abck` whose sensor field is `-1` has not been validated yet
(no accepted sensor POST). `is_abck_valid` treats that as not-yet-solved."""


def is_abck_valid(abck_cookie_value: str | None) -> bool:
    """True when an `_abck` cookie looks validated (present and not carrying
    the `~-1~` not-yet-solved sensor marker). Used by the warm-up loop to
    decide whether enough human telemetry has been accepted before reading."""

    if not abck_cookie_value:
        return False
    return _ABCK_INVALID.search(abck_cookie_value) is None


def has_known_wall_marker(html: str | None) -> bool:
    """Whether the body carries a marker the generic classifier recognises as a
    definite wall (Akamai / Cloudflare / DataDome / PerimeterX).

    Exists for site checkers: they run *before* the generic markers (Pulsar's
    `addFirst`), so a checker whose own signal is a weak heuristic -- a page-size
    floor, say -- must defer on a page the generic path can classify precisely.
    Otherwise a size floor downgrades a hard `FORBIDDEN` (PRIVACY scope, instant
    burn) to a soft `TOO_SMALL` (CRAWL scope, same-identity retry), and the
    identity keeps hammering a wall it has already been told about.
    """

    body = (html or "").lower()
    if any(
        m in body
        for group in (_AKAMAI_MARKERS, _CHALLENGE_MARKERS, _DATADOME_MARKERS, _PERIMETERX_MARKERS)
        for m in group
    ):
        return True
    return _short_page_wall_marker(body)


def _short_page_wall_marker(body: str) -> bool:
    """A vendor name or a challenge prompt, on a page too short to be content.

    See `_VENDOR_PRESENCE_MARKERS`: on a full-size page these prove only that
    the site is protected, so they must not be read as a wall there.
    """

    if len(body) >= _VENDOR_PRESENCE_MAX_LEN:
        return False
    return any(
        m in body for group in (_VENDOR_PRESENCE_MARKERS, _AMBIGUOUS_HOLD_MARKERS) for m in group
    )


class SiteChecker(Protocol):
    """A per-site block/integrity checker -- a port of Pulsar's
    `HtmlIntegrityChecker` (`HtmlIntegrityChecker.kt:27-30`). `is_relevant`
    gates it by URL; `check` returns a `Verdict` when it has an opinion (a
    site-specific redirect/captcha/undersize tell) or `None` to defer to the
    next checker / the generic classifier. Registered checkers run *before* the
    generic markers (Pulsar's `addFirst`), so a site's own definitive signal
    (e.g. Walmart's `/blocked` wall -> ROBOT_CHECK_3) wins."""

    def is_relevant(self, url: str) -> bool: ...

    def check(self, *, html: str | None, url: str, status: int | None) -> Verdict | None: ...


def _site_verdict(
    hooks: BlockHooks | None, html: str | None, url: str, status: int | None
) -> Verdict | None:
    """First non-OK verdict from a relevant extension, or `None` if none has an
    opinion (`ChainedHtmlIntegrityChecker.kt:90-96`).

    The chain is *passed in*, not read from module state. It used to be a
    module-level `_SITE_CHECKERS` list populated by an
    `install_default_site_checkers()` call at import time, which made merely
    importing this module register three retailers' policy globally and made
    behaviour depend on import order (plan D12).
    """

    if hooks is None:
        return None
    verdict: Verdict | None = hooks.classify.first_result(
        html=html, url=url, status=status
    )
    if verdict is not None and verdict is not Verdict.OK:
        return verdict
    return None


def _header_verdict(headers: Mapping[str, str] | None) -> Verdict | None:
    """A WAF identified by its own response headers.

    `x-datadome`/`x-px-block` are set on served *and* blocked responses by some
    deployments, so presence alone is not a block -- the value is. DataDome
    marks a challenge with `x-datadome: protected` plus a 403, and PerimeterX
    sets `x-px-block: 1`. When the header is present but the status is fine, we
    defer to the body checks rather than crying wolf.
    """

    if not headers:
        return None
    lowered = {k.lower(): (v or "").lower() for k, v in headers.items()}

    if any(h in lowered for h in _PERIMETERX_HEADERS):
        if lowered.get("x-px-block") == "1":
            return Verdict.ROBOT_CHECK
    if any(h in lowered for h in _DATADOME_HEADERS):
        # A DataDome deployment tags every response; only a 4xx alongside it
        # means this particular one was refused.
        if lowered.get("x-datadome") in ("protected", "blocked"):
            return Verdict.ROBOT_CHECK
    if "set-cookie" in lowered and "datadome=" in lowered["set-cookie"]:
        # Not a block by itself -- just proof the site is DataDome-protected,
        # which the body markers below then interpret.
        return None
    return None


def classify_page(
    *,
    html: str | None,
    url: str,
    status: int | None,
    headers: Mapping[str, str] | None = None,
    hooks: BlockHooks | None = None,
) -> Verdict:
    """Pure classifier: `(html, url, status, headers) -> Verdict`. Status is the
    primary signal when present, but body markers win for the 200-body walls
    Akamai, Cloudflare and DataDome serve. Order matters -- hard status, then
    response headers, then per-site checkers, then the generic provider walls,
    then the empty/too-small fallbacks, then OK.

    `url` should be the page's *final* location as the browser computed it, not
    the URL that was requested: a WAF commonly 200-redirects a bot to a block
    page without the fetcher ever seeing a redirect status, and only the landed
    URL betrays it (Pulsar's `WalmartHtmlChecker` reads `activeDOMUrls.location`
    for exactly this reason).

    `headers` is optional so existing callers keep working, but passing it is
    what catches a challenge that is visually indistinguishable from a thin
    real page.
    """

    body = (html or "").lower()

    # Hard status signals first.
    if status == 404:
        return Verdict.NOT_FOUND
    if status == 429:
        return Verdict.RATE_LIMITED

    # Response headers -- unambiguous, and invisible to every body heuristic.
    header_verdict = _header_verdict(headers)
    if header_verdict is not None:
        return header_verdict

    # Per-site checkers (Pulsar addFirst): host-aware redirect/captcha/undersize
    # tells that the generic markers can't see (silent redirects to a block URL,
    # per-page-type minimum content size).
    site_verdict = _site_verdict(hooks, html, url, status)
    if site_verdict is not None:
        return site_verdict

    # Akamai / Cloudflare walls -- often 200, so check the body regardless.
    if any(m in body for m in _AKAMAI_MARKERS):
        return Verdict.FORBIDDEN
    if is_akamai_challenge(body):
        # Still on the sensor stub at read time: the warm-up already waited for
        # it to reload (`warmup.settle_akamai_challenge`), so this one did not
        # pass. A challenge, not a hard deny -- the lightest robot-check weight.
        return Verdict.ROBOT_CHECK
    if any(m in body for m in _CHALLENGE_MARKERS):
        return Verdict.ROBOT_CHECK
    if any(m in body for m in _DATADOME_MARKERS):
        return Verdict.ROBOT_CHECK
    if any(m in body for m in _PERIMETERX_MARKERS):
        return Verdict.ROBOT_CHECK
    # Vendor names and the Press & Hold prompt, but only on a page too short to
    # be real content -- see `_VENDOR_PRESENCE_MARKERS` for the served Walmart
    # PDP that made this gate necessary.
    if _short_page_wall_marker(body):
        return Verdict.ROBOT_CHECK

    # Generic redirect-to-block / forbidden-body tells (a site checker upgrades
    # these to a heavier severity for hosts it knows; this is the fallback for
    # everything else). WalmartCrawler.kt:33-41.
    lurl = url.lower()
    if "/blocked" in lurl or "blocked?" in lurl or "/verify" in lurl:
        return Verdict.ROBOT_CHECK
    if "403 forbidden" in body:
        return Verdict.FORBIDDEN

    # Generic forbidden after body checks (a 403 with no known wall markers).
    if status == 403:
        return Verdict.FORBIDDEN

    # Empty / thin-content fallbacks.
    if not body.strip():
        return Verdict.EMPTY
    if "<a" not in body and len(body) < _TOO_SMALL_LEN:
        return Verdict.EMPTY
    if len(body) < _TOO_SMALL_LEN:
        return Verdict.TOO_SMALL

    return Verdict.OK


# Weighted burn accounting (AbstractPrivacyContext.kt:330-347). A context is
# retired once accumulated warnings reach MAX_WARNINGS; a success decrements by
# 1 (self-healing). FORBIDDEN is an instant retire (weight == MAX_WARNINGS).
MAX_WARNINGS = 8

# Weights ported from `AbstractPrivacyContext.afterRun` (:311-346): robot checks
# 1/2/3 by severity, wrong-geo +2, empty +3, forbidden an instant retire.
_WARNING_WEIGHT = {
    Verdict.OK: 0,
    Verdict.NOT_FOUND: 0,
    Verdict.TOO_SMALL: 1,
    Verdict.WRONG_GEO: 2,
    Verdict.EMPTY: 3,
    Verdict.RATE_LIMITED: 2,
    Verdict.ROBOT_CHECK: 1,
    Verdict.ROBOT_CHECK_2: 2,
    Verdict.ROBOT_CHECK_3: 3,
    Verdict.FORBIDDEN: MAX_WARNINGS,  # instant retire
}

# Scope ported from `BrowserResponseHandlerImpl.createProtocolStatusForBrokenContent`
# (:143-163): robot/forbidden/empty tear the identity down (PRIVACY); small /
# rate-limited / wrong-geo are cheap same-identity retries (CRAWL).
_SCOPE = {
    Verdict.OK: Scope.NONE,
    Verdict.NOT_FOUND: Scope.NONE,
    Verdict.TOO_SMALL: Scope.CRAWL,
    Verdict.WRONG_GEO: Scope.CRAWL,
    Verdict.RATE_LIMITED: Scope.CRAWL,
    Verdict.EMPTY: Scope.PRIVACY,
    Verdict.ROBOT_CHECK: Scope.PRIVACY,
    Verdict.ROBOT_CHECK_2: Scope.PRIVACY,
    Verdict.ROBOT_CHECK_3: Scope.PRIVACY,
    Verdict.FORBIDDEN: Scope.PRIVACY,
}


def warning_weight(verdict: Verdict) -> int:
    return _WARNING_WEIGHT.get(verdict, 0)


def retry_scope(verdict: Verdict) -> Scope:
    return _SCOPE.get(verdict, Scope.NONE)


def is_blocked(verdict: Verdict) -> bool:
    """A hard wall (PRIVACY scope): the robot/forbidden/empty family that should
    raise `ChallengeDetected` and rotate the whole identity, not the soft
    too-small/wrong-geo/rate-limited signals a same-identity retry may recover
    from (those are surfaced as CRAWL-scope soft verdicts instead)."""

    return retry_scope(verdict) is Scope.PRIVACY

