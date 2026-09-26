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

import json
import re

import httpx

from crawlpilot.egress.httpx_guard import assert_host_allowed
from crawlpilot.extensions.mounts import BlockHooks
from crawlpilot.extraction import block_detect, entities, pdf
from crawlpilot.extraction.extractor import extract
from crawlpilot.spi.actions import ActionResult, ExtractFormat
from crawlpilot.spi.egress import EgressPolicy
from crawlpilot.spi.errors import ChallengeDetected
from crawlpilot.spi.proxy import ProxyEndpoint
from crawlpilot.spi.scrape import ScrapeOptions

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
    egress: EgressPolicy | None = None,
    block_hooks: BlockHooks | None = None,
) -> ActionResult:
    """GET `url` over HTTP, classify the response, and extract the requested
    formats -- returning an `ActionResult` matching the browser path's shape
    (`extracts` index-correlated to `formats`, `page_title`, `status_code`,
    `soft_verdict`). Raises `ChallengeDetected` (scope `"privacy"`) on a hard
    wall so the caller escalates to the browser; a soft (CRAWL) verdict is
    recorded on the result and the content returned."""

    # SSRF guard. This module is the `basic` tier's fetcher and
    # `egress/httpx_guard` is documented as that tier's protection, but nothing
    # had ever called it: `guarded_get`/`assert_host_allowed` had no production
    # caller at all, so a scrape of `http://169.254.169.254/` went straight out.
    # The browser path is covered separately by the container-wide iptables
    # baseline (`egress/policy.apply_baseline`), which this path never touches
    # because it never opens a browser.
    #
    # Skipped when the caller injected a client: that is the test seam, and the
    # tests point at a local `MockTransport`/loopback server the guard would
    # (correctly) refuse.
    if client is None:
        assert_host_allowed(httpx.URL(url).host, egress or EgressPolicy())

    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(
            proxy=proxy_url(proxy) if proxy else None,
            follow_redirects=True,
            timeout=timeout_ms / 1000,
            # Chrome always negotiates h2. Offering only HTTP/1.1 while
            # claiming to be Chrome is a tell on its own, independent of the
            # HTTP/2 SETTINGS-frame fingerprint a WAF may go on to compare.
            http2=True,
        )
    try:
        resp = await client.get(url, headers=headers)
    finally:
        if owns_client:
            await client.aclose()

    status = resp.status_code
    final_url = str(resp.url)

    if pdf.is_pdf(content_type=resp.headers.get("content-type"), body=resp.content):
        # Short-circuit before block detection and the HTML pipeline, both of
        # which are meaningless for a binary document: `classify_page` would be
        # reading PDF bytes for wall markers, and `sanitize` would hand lxml a
        # file it cannot parse.
        return _pdf_result(resp.content, formats=formats, status=status)

    html = resp.text

    verdict = block_detect.classify_page(
        html=html, url=final_url, status=status, hooks=block_hooks
    )
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
                relevance_query=options.relevance_query,
                citations=options.citations,
            )
        )
    return result


def _pdf_result(
    body: bytes, *, formats: tuple[ExtractFormat, ...], status: int
) -> ActionResult:
    """An `ActionResult` for a PDF, shaped exactly like the HTML path's.

    `extracts` stays index-correlated with `formats` -- every requested format
    gets a slot, even the ones a PDF cannot answer. A caller who asked for
    `["markdown", "structured_data"]` gets the text and an empty JSON object,
    rather than a short list that would silently misalign every format after the
    missing one.
    """

    metadata = pdf.pdf_metadata(body)
    result = ActionResult(status_code=status, page_title=metadata.get("title"))

    try:
        markdown = pdf.extract_pdf(body, format="markdown")
        text = pdf.extract_pdf(body, format="text")
        error: str | None = None
    except pdf.PdfSupportUnavailable as exc:
        markdown = text = ""
        error = str(exc)

    for fmt in formats:
        if fmt in ("markdown", "fit_markdown"):
            # `fit_markdown` is the same text: there is no boilerplate in a PDF to
            # prune, and the density scoring that produces it operates on a DOM.
            result.extracts.append(markdown)
        elif fmt == "text":
            result.extracts.append(text)
        elif fmt == "html":
            # Deliberately empty rather than a fabricated wrapper. A caller asking
            # for `html` wants the document as served, and this document has none.
            result.extracts.append("")
        elif fmt == "structured_data":
            result.extracts.append(
                json.dumps({"metadata": metadata, "json_ld": [], "hydration": {}})
            )
        elif fmt == "entities":
            result.extracts.append(json.dumps(entities.extract_entities(text)))
        elif fmt == "tables":
            # A PDF's tables are a layout problem, not a markup one -- there is no
            # `<table>` to read. Extracting them needs column-position clustering,
            # which is a separate piece of work rather than a gap to paper over.
            result.extracts.append(json.dumps([]))
        else:
            result.extracts.append("")

    if error is not None:
        result.verifications.append(error)
    return result
