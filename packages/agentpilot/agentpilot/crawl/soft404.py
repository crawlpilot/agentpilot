"""Soft-404 detection: recognising the "not found" page a site serves with
HTTP 200.

Single-page apps do this almost universally. The server has no idea which routes
the client-side router knows about, so it returns the same 200 shell for
`/products/real-thing` and `/products/nonsense-that-never-existed`, and the
difference appears only after JavaScript runs. Any discovery source that guesses
at paths -- probing, Wayback (which has archived URLs that stopped existing years
ago), Common Crawl (same) -- therefore produces URLs that look alive and are not.

The trick, taken from crawl4ai's `DomainMapper` (Apache-2.0; crawl4ai 0.9.4),
is to ask the site for a URL that certainly does not exist, keep what it says,
and then treat any response matching that as not-found. It costs one request per
host and removes a whole class of junk from the result set.

Two deliberate differences from upstream:

1. **Hashes a normalized prefix, not raw bytes.** Upstream MD5s the first 2 KB
   of the body as-is, which means a single nonce in the markup -- a CSRF token, a
   request id, a cache-buster in an asset URL, a timestamp -- makes every
   not-found page hash differently and the check silently never fires. Collapsing
   whitespace and stripping the digits that vary is what makes it work on a real
   site.
2. **Records the redirect target.** A large share of SPAs answer an unknown path
   with a 302 to `/` or `/404`. Upstream follows redirects and compares bodies,
   which works, but comparing the final URL is both cheaper and more obviously
   correct.

Uses `blake2b`, not MD5 -- same speed class, and no reviewer has to pause over
why a hash function with known collisions is in the codebase. Nothing here is
security-sensitive; it is simply the one that does not raise the question.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass

import httpx

from crawlpilot.egress.httpx_guard import guarded_get
from crawlpilot.spi.egress import EgressPolicy
from crawlpilot.spi.errors import EgressBlocked

_TITLE_RE = re.compile(rb"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_SAMPLE_BYTES = 4096
"""How much of the body to fingerprint. Enough to reach past `<head>` into the
start of the visible page on a typical document, and bounded so a probe against a
host serving multi-megabyte HTML stays cheap."""

_VARIABLE = re.compile(rb"\d+")
"""Digits are where per-response nonces live -- tokens, ids, timestamps,
cache-busting query strings. Removing them before hashing is the difference
between this check working on a real site and never firing at all."""


@dataclass(frozen=True)
class Soft404Fingerprint:
    """What a host returns for a URL that does not exist."""

    status_code: int
    title: str | None
    body_hash: str
    final_path: str | None
    """Where a redirect landed, if the probe was redirected. A host that sends
    every unknown path to `/404` is identified by this alone."""


def _normalized_hash(body: bytes) -> str:
    sample = body[:_SAMPLE_BYTES]
    collapsed = b" ".join(sample.split())
    return hashlib.blake2b(_VARIABLE.sub(b"", collapsed), digest_size=16).hexdigest()


def _title_of(body: bytes) -> str | None:
    match = _TITLE_RE.search(body[:_SAMPLE_BYTES])
    if match is None:
        return None
    return match.group(1).decode("utf-8", "replace").strip() or None


async def fingerprint(
    origin: str, policy: EgressPolicy, *, timeout_seconds: float = 10.0
) -> Soft404Fingerprint | None:
    """Ask `origin` for a URL that cannot exist, and keep the answer.

    `None` when the host behaves correctly (a real 404/410) or the probe failed
    -- in both cases there is nothing to compare against later, and
    `is_soft_404` will pass everything through. Failing open is right: the cost
    of missing the detection is some junk in the result set, and the cost of
    getting it wrong is dropping real pages.
    """

    probe_path = f"/{uuid.uuid4().hex[:16]}-probe-does-not-exist"
    try:
        response = await guarded_get(
            f"{origin.rstrip('/')}{probe_path}",
            policy,
            timeout=timeout_seconds,
            follow_redirects=True,
        )
    except (EgressBlocked, httpx.HTTPError):
        return None

    if response.status_code in (404, 410):
        # The host is honest about missing pages, so there is no soft-404 shape
        # to learn and nothing to filter on.
        return None
    if response.status_code >= 500:
        # A 5xx on the probe says the host is unwell, not that it serves
        # soft-404s. Fingerprinting it would teach us to discard real pages the
        # moment it recovers.
        return None

    body = response.content
    final_path = None
    if str(response.url).rstrip("/") != f"{origin.rstrip('/')}{probe_path}":
        final_path = httpx.URL(str(response.url)).path or "/"

    return Soft404Fingerprint(
        status_code=response.status_code,
        title=_title_of(body),
        body_hash=_normalized_hash(body),
        final_path=final_path,
    )


def is_soft_404(
    *,
    status_code: int,
    body: bytes,
    final_path: str | None,
    fingerprint: Soft404Fingerprint | None,
) -> bool:
    """Whether this response is the host's not-found page wearing a 200.

    Three independent matches, any of which is enough: the same redirect
    destination, the same normalized body, or the same `<title>`. Titles are
    checked last and only when non-empty -- two pages sharing a blank title says
    nothing at all.
    """

    if fingerprint is None:
        return False
    if status_code != fingerprint.status_code:
        return False

    if fingerprint.final_path is not None and final_path == fingerprint.final_path:
        return True
    if _normalized_hash(body) == fingerprint.body_hash:
        return True
    probe_title = fingerprint.title
    return bool(probe_title and _title_of(body) == probe_title)
