"""The `basic`-tier HTTP fast-path: fetch a page with `httpx` (no browser) and
run it through the same block-detection + extraction pipeline the browser path
uses, returning an `ActionResult` shaped exactly like `driver.execute` would.

This is the long-documented-but-missing cheap path (`humanize.py` / `ephemeral.py`
both referred to a "basic never reaches the browser (httpx path)" that didn't
exist). It mirrors Pulsar's philosophy of reusing a lightweight HTTP session for
content that doesn't need a full browser (`AbstractWebDriver`'s jsoup-session
reuse). On a hard block it raises `ChallengeDetected` so the scrape ladder can
escalate to the real browser (`stealth`).

Kept deliberately dependency-injectable: `fetch_via_http` accepts an optional
`client`, so tests drive it with an `httpx.MockTransport` and never touch the
network or a real proxy.
"""

from __future__ import annotations

import re

import httpx

from agentpilot.extraction import block_detect
from agentpilot.extraction.extractor import extract
from agentpilot.spi.actions import ActionResult, ExtractFormat
from agentpilot.spi.errors import ChallengeDetected
from agentpilot.spi.proxy import ProxyEndpoint
from agentpilot.spi.scrape import ScrapeOptions

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def proxy_url(proxy: ProxyEndpoint) -> str:
    """`scheme://[user:pass@]host:port` for httpx's `proxy=` kwarg."""
    auth = ""
    if proxy.username:
        auth = proxy.username
        if proxy.password:
            auth += f":{proxy.password}"
        auth += "@"
    return f"{proxy.scheme}://{auth}{proxy.host}:{proxy.port}"


def _title(html: str) -> str | None:
    m = _TITLE_RE.search(html)
    return m.group(1).strip() if m else None


async def fetch_via_http(
    *,
    url: str,
    formats: tuple[ExtractFormat, ...],
    options: ScrapeOptions,
    headers: dict[str, str],
    proxy: ProxyEndpoint | None = None,
    timeout_ms: int = 30_000,
    client: httpx.AsyncClient | None = None,
) -> ActionResult:
    """GET `url` over HTTP, classify the response, and extract the requested
    formats -- returning an `ActionResult` matching the browser path's shape
    (`extracts` index-correlated to `formats`, `page_title`, `status_code`,
    `soft_verdict`). Raises `ChallengeDetected` (scope `"privacy"`) on a hard
    wall so the caller escalates to the browser; a soft (CRAWL) verdict is
    recorded on the result and the content returned."""

    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(
            proxy=proxy_url(proxy) if proxy else None,
            follow_redirects=True,
            timeout=timeout_ms / 1000,
        )
    try:
        resp = await client.get(url, headers=headers)
    finally:
        if owns_client:
            await client.aclose()

    html = resp.text
    status = resp.status_code
    final_url = str(resp.url)

    verdict = block_detect.classify_page(html=html, url=final_url, status=status)
    weight = block_detect.warning_weight(verdict)
    scope = block_detect.retry_scope(verdict)
    if scope is block_detect.Scope.PRIVACY:
        raise ChallengeDetected(
            f"{verdict.value} at {final_url} (http)",
            verdict=verdict.value,
            weight=weight,
            scope="privacy",
        )

    result = ActionResult(status_code=status, page_title=_title(html))
    if scope is block_detect.Scope.CRAWL:
        result.soft_verdict = verdict.value
        result.soft_weight = weight
    for fmt in formats:
        result.extracts.append(
            extract(
                html,
                format=fmt,
                main_content=options.only_main_content,
                include_tags=options.include_tags,
                exclude_tags=options.exclude_tags,
                base_url=final_url,
            )
        )
    return result
