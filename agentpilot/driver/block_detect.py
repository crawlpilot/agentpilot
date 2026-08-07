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


class Verdict(enum.Enum):
    OK = "ok"
    ROBOT_CHECK = "robot_check"  # CAPTCHA / JS challenge / Akamai sensor wall
    FORBIDDEN = "forbidden"  # hard 403 / Access Denied
    RATE_LIMITED = "rate_limited"  # 429
    WRONG_GEO = "wrong_geo"  # served, but wrong country/district/lang
    TOO_SMALL = "too_small"  # rendered, but suspiciously thin
    EMPTY = "empty"  # blank / no-body / no-anchor
    NOT_FOUND = "not_found"  # genuine 404, do not retry


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

# --- Cloudflare / hCaptcha / generic interstitials.
_CHALLENGE_MARKERS = (
    "just a moment",
    "cf-browser-verification",
    "cf-challenge",
    "hcaptcha",
    "/cdn-cgi/challenge-platform",
    "attention required",
)

# --- Amazon-style CAPTCHA (AmazonHtmlIntegrityChecker.kt:120): a short page
# carrying this exact prompt is a robot check.
_AMAZON_CAPTCHA = "type the characters you see in this image"
_AMAZON_ROBOT_MAX_LEN = 150_000

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


def classify_page(
    *,
    html: str | None,
    url: str,
    status: int | None,
) -> Verdict:
    """Pure classifier: `(html, url, status) -> Verdict`. Status is the primary
    signal when present, but body markers win for the 200-body walls Akamai and
    Cloudflare serve. Order matters -- most-specific block signals first, then
    the generic empty/too-small fallbacks, then OK."""

    body = (html or "").lower()
    lurl = url.lower()

    # Hard status signals first.
    if status == 404:
        return Verdict.NOT_FOUND
    if status == 429:
        return Verdict.RATE_LIMITED

    # Akamai / Cloudflare walls -- often 200, so check the body regardless.
    if any(m in body for m in _AKAMAI_MARKERS):
        return Verdict.FORBIDDEN
    if any(m in body for m in _CHALLENGE_MARKERS):
        return Verdict.ROBOT_CHECK

    # Walmart-style URL / body tells (WalmartCrawler.kt:33-41).
    if "blocked" in lurl:
        return Verdict.ROBOT_CHECK
    if "403 forbidden" in body:
        return Verdict.FORBIDDEN

    # Amazon-style CAPTCHA: a *short* page carrying the prompt.
    if len(body) < _AMAZON_ROBOT_MAX_LEN and _AMAZON_CAPTCHA in body:
        return Verdict.ROBOT_CHECK

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

_WARNING_WEIGHT = {
    Verdict.OK: 0,
    Verdict.NOT_FOUND: 0,
    Verdict.TOO_SMALL: 1,
    Verdict.WRONG_GEO: 2,
    Verdict.EMPTY: 3,
    Verdict.RATE_LIMITED: 2,
    Verdict.ROBOT_CHECK: 2,
    Verdict.FORBIDDEN: MAX_WARNINGS,  # instant retire
}

_SCOPE = {
    Verdict.OK: Scope.NONE,
    Verdict.NOT_FOUND: Scope.NONE,
    Verdict.TOO_SMALL: Scope.CRAWL,
    Verdict.WRONG_GEO: Scope.CRAWL,
    Verdict.RATE_LIMITED: Scope.CRAWL,
    Verdict.EMPTY: Scope.PRIVACY,
    Verdict.ROBOT_CHECK: Scope.PRIVACY,
    Verdict.FORBIDDEN: Scope.PRIVACY,
}


def warning_weight(verdict: Verdict) -> int:
    return _WARNING_WEIGHT.get(verdict, 0)


def retry_scope(verdict: Verdict) -> Scope:
    return _SCOPE.get(verdict, Scope.NONE)


def is_blocked(verdict: Verdict) -> bool:
    """A verdict that should raise `ChallengeDetected` rather than return
    content -- the robot/forbidden/challenge family, not the soft
    too-small/wrong-geo signals a retry might still recover from."""

    return verdict in (Verdict.ROBOT_CHECK, Verdict.FORBIDDEN)
