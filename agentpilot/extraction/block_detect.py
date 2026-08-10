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
from typing import Protocol


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

# --- Cloudflare / hCaptcha / generic interstitials.
_CHALLENGE_MARKERS = (
    "just a moment",
    "cf-browser-verification",
    "cf-challenge",
    "hcaptcha",
    "/cdn-cgi/challenge-platform",
    "attention required",
)

# Amazon-style CAPTCHA and other site-specific tells now live in
# `agentpilot.extraction.site_checkers` (installed into the chain at the bottom
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


_SITE_CHECKERS: list[SiteChecker] = []


def register_site_checker(checker: SiteChecker) -> None:
    """Append a per-site checker to the chain (Pulsar `ChainedHtmlIntegrityChecker
    .addLast`). Idempotent per instance is not enforced -- callers install once."""
    _SITE_CHECKERS.append(checker)


def _site_verdict(html: str | None, url: str, status: int | None) -> Verdict | None:
    """First non-OK verdict from a relevant site checker, or `None` if none has
    an opinion (`ChainedHtmlIntegrityChecker.kt:90-96`)."""
    for checker in _SITE_CHECKERS:
        if not checker.is_relevant(url):
            continue
        verdict = checker.check(html=html, url=url, status=status)
        if verdict is not None and verdict is not Verdict.OK:
            return verdict
    return None


def classify_page(
    *,
    html: str | None,
    url: str,
    status: int | None,
) -> Verdict:
    """Pure classifier: `(html, url, status) -> Verdict`. Status is the primary
    signal when present, but body markers win for the 200-body walls Akamai and
    Cloudflare serve. Order matters -- hard status, then per-site checkers, then
    the generic provider walls, then the empty/too-small fallbacks, then OK."""

    body = (html or "").lower()

    # Hard status signals first.
    if status == 404:
        return Verdict.NOT_FOUND
    if status == 429:
        return Verdict.RATE_LIMITED

    # Per-site checkers (Pulsar addFirst): host-aware redirect/captcha/undersize
    # tells that the generic markers can't see (silent redirects to a block URL,
    # per-page-type minimum content size).
    site_verdict = _site_verdict(html, url, status)
    if site_verdict is not None:
        return site_verdict

    # Akamai / Cloudflare walls -- often 200, so check the body regardless.
    if any(m in body for m in _AKAMAI_MARKERS):
        return Verdict.FORBIDDEN
    if any(m in body for m in _CHALLENGE_MARKERS):
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


# Install the built-in per-site checkers into the chain. Done at the bottom so
# every name `site_checkers` imports from this module is already defined; the
# import is deferred here (not at the top) to avoid a circular import.
from agentpilot.extraction import site_checkers as _site_checkers  # noqa: E402

_site_checkers.install_default_site_checkers()
