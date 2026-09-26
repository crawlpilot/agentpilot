"""Head-only metadata fetch: a title and description per discovered URL, for a
fraction of the cost of scraping it.

`MapLink` has carried `title` and `description` fields since it was written and
nothing ever filled them, because the only thing that knew a page's title was a
full scrape. That is the wrong trade for `/v1/map`, whose whole proposition is
"thousands of URLs, fast": a caller looking at a map wants to know which of the
900 results is the pricing page, and the answer is in the first two kilobytes of
each document.

So this reads only that. A `GET` with a byte-range request and a hard cap on how
much of the body is consumed, parsed with the extractor the platform already has
-- `crawlpilot.extraction.structured_data.extract_meta` -- rather than a second
metadata parser written to live here.

Adapted from crawl4ai's `AsyncUrlSeeder._fetch_head` / `_parse_head`
(Apache-2.0; crawl4ai 0.9.4). Three differences:

1. **Reuses the platform's metadata reader.** Upstream ships its own `_parse_head`
   with an lxml path and a regex fallback. `extraction.structured_data` already
   does that job, better (it merges OpenGraph, Twitter and Dublin Core, and
   resolves relative URLs), and having two parsers means fixing a metadata bug
   twice.
2. **Streams and stops.** Upstream fetches the whole response and then slices. A
   `<head>` is in the first few KB; downloading a 4 MB document to read 2 KB of
   it, thousands of times, is the entire cost this module exists to avoid.
3. **One bounded pass, rate-limited by the caller.** Upstream defaults to 1000
   concurrent requests. A thousand parallel sockets aimed at one host is not a
   metadata fetch, it is a load test.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx

from crawlpilot.extraction import structured_data
from crawlpilot.spi.egress import EgressPolicy
from crawlpilot.spi.errors import EgressBlocked

HEAD_BYTES = 16_384
"""How much of each document to read. Generous by `<head>` standards -- a heavy
commercial page carries a lot of preload hints and inline JSON-LD before its
`<title>` -- and still ~1% of a rendered page's weight."""

DEFAULT_CONCURRENCY = 12
"""Per call, across all hosts. Low on purpose: this runs against one site's URL
list, so the concurrency number *is* the per-host concurrency."""


@dataclass(frozen=True)
class HeadMetadata:
    url: str
    status_code: int | None
    title: str | None
    description: str | None
    alive: bool
    """Whether the URL answered at all with a non-error status. This is the
    live-check upstream offers as a separate `live_check` pass -- fetching the
    head already proves liveness, so paying for a second HEAD request to learn
    the same thing would be wasted."""


async def fetch_many(
    urls: list[str],
    policy: EgressPolicy,
    *,
    client: httpx.AsyncClient | None = None,
    concurrency: int = DEFAULT_CONCURRENCY,
    timeout_seconds: float = 10.0,
) -> dict[str, HeadMetadata]:
    """Metadata for as many of `urls` as answer, keyed by URL.

    Never raises for an individual URL: one that times out, 404s or is blocked by
    the egress guard comes back with `alive=False` and no title. A map of a
    thousand URLs should not fail because three of them are dead.
    """

    if not urls:
        return {}

    semaphore = asyncio.Semaphore(max(concurrency, 1))
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(follow_redirects=True, http2=True)

    async def one(url: str) -> HeadMetadata:
        async with semaphore:
            return await fetch_one(url, policy, client=client, timeout_seconds=timeout_seconds)

    try:
        results = await asyncio.gather(*(one(url) for url in urls))
    finally:
        if owns_client:
            await client.aclose()

    return {result.url: result for result in results}


async def fetch_one(
    url: str,
    policy: EgressPolicy,
    *,
    client: httpx.AsyncClient,
    timeout_seconds: float = 10.0,
) -> HeadMetadata:
    from crawlpilot.egress.httpx_guard import assert_host_allowed

    try:
        assert_host_allowed(httpx.URL(url).host, policy)
    except (EgressBlocked, ValueError, UnicodeError):
        return HeadMetadata(url=url, status_code=None, title=None, description=None, alive=False)

    try:
        # `Range` is a hint, not a contract -- plenty of servers ignore it and
        # send the whole document. The streaming loop below is what actually
        # bounds the transfer; the header just lets a well-behaved origin save
        # both sides the bytes.
        async with client.stream(
            "GET",
            url,
            timeout=timeout_seconds,
            headers={"Range": f"bytes=0-{HEAD_BYTES - 1}"},
        ) as response:
            if response.status_code >= 400:
                return HeadMetadata(
                    url=url,
                    status_code=response.status_code,
                    title=None,
                    description=None,
                    alive=False,
                )
            content_type = response.headers.get("content-type", "")
            if content_type and "html" not in content_type.lower():
                # A PDF or an image has no `<head>` to read. It answered, so it
                # is alive; there is just no metadata here.
                return HeadMetadata(
                    url=url,
                    status_code=response.status_code,
                    title=None,
                    description=None,
                    alive=True,
                )

            chunks: list[bytes] = []
            read = 0
            async for chunk in response.aiter_bytes():
                chunks.append(chunk)
                read += len(chunk)
                if read >= HEAD_BYTES:
                    break
            body = b"".join(chunks)[:HEAD_BYTES]
            status = response.status_code
            final_url = str(response.url)
    except (httpx.HTTPError, EgressBlocked):
        return HeadMetadata(url=url, status_code=None, title=None, description=None, alive=False)

    title, description = _title_and_description(body, base_url=final_url)
    return HeadMetadata(
        url=url, status_code=status, title=title, description=description, alive=True
    )


def _title_and_description(body: bytes, *, base_url: str) -> tuple[str | None, str | None]:
    """Pulled out of `extraction.structured_data`'s metadata bundle.

    A truncated document is the normal case here -- the body was cut mid-tag on
    purpose -- so anything that raises while parsing is expected rather than
    exceptional, and yields no metadata instead of failing the URL.
    """

    try:
        decoded = body.decode("utf-8", "replace")
        bundle = structured_data.extract_structured_data(decoded, base_url=base_url)
    except Exception:
        return None, None

    meta = bundle.get("metadata") or {}
    title = meta.get("title") or meta.get("og:title") or meta.get("twitter:title")
    description = (
        meta.get("description") or meta.get("og:description") or meta.get("twitter:description")
    )
    return (
        title.strip() if isinstance(title, str) and title.strip() else None,
        description.strip() if isinstance(description, str) and description.strip() else None,
    )
