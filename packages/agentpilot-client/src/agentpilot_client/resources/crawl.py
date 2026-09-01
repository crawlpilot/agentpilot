"""`/v1/crawl` -- queued, many pages, polled.

The job endpoints all share a shape: create returns an id, status is polled, and
the result arrives in pages. `CrawlJob` wraps that so a caller writes
`.wait()` instead of a polling loop, while `.status()` stays available for
anyone driving their own.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from agentpilot_client.resources.scrape import document_from_wire
from crawlpilot.spi.errors import DriverError
from crawlpilot.spi.scrape import Document

if TYPE_CHECKING:
    from agentpilot_client._transport import Transport

TERMINAL = frozenset({"completed", "failed", "cancelled"})


class CrawlFailed(DriverError):
    """A crawl reached a terminal state that was not `completed`.

    A `DriverError` so it lands in the same `except` a caller already writes for
    everything else this client raises.
    """


class CrawlJob:
    """A queued crawl. Poll it, wait on it, or cancel it."""

    def __init__(self, transport: Transport, job_id: str, *, webhook_secret: str | None = None):
        self._transport = transport
        self.id = job_id
        self.webhook_secret = webhook_secret
        """Returned exactly once, when the request set a webhook. Never
        retrievable again -- same "shown once" discipline as an API key."""

    async def status(self, *, after: str | None = None, limit: int | None = None) -> dict[str, Any]:
        """The raw status page: `status`, `total`, `completed`, `failed`,
        `data`, and a `next` cursor."""

        params: dict[str, Any] = {}
        if after is not None:
            params["after"] = after
        if limit is not None:
            params["limit"] = limit
        return await self._transport.request(
            "GET", f"/v1/crawl/{self.id}", params=params or None
        )

    async def documents(self) -> list[Document]:
        """Every document completed so far, following the cursor to the end.

        Paginated rather than returned whole because a large crawl's result set
        does not fit in one response -- and following `next` here is the part
        every caller would otherwise write themselves.
        """

        out: list[Document] = []
        cursor: str | None = None
        while True:
            page = await self.status(after=cursor)
            out.extend(document_from_wire(d) for d in page.get("data", []))
            cursor = page.get("next")
            if not cursor:
                return out

    async def wait(
        self, *, poll_interval: float = 2.0, timeout: float | None = None
    ) -> list[Document]:
        """Block until the crawl reaches a terminal state, then return its
        documents.

        Raises `CrawlFailed` on `failed`/`cancelled` rather than returning a
        partial list that looks like a complete one -- the caller asked for a
        crawl, and silently handing back half of it is how a pipeline reports
        success on missing data.
        """

        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            page = await self.status()
            state = page.get("status")
            if state in TERMINAL:
                if state != "completed":
                    raise CrawlFailed(
                        f"crawl {self.id} ended as {state!r} after "
                        f"{page.get('completed', 0)}/{page.get('total', 0)} pages"
                    )
                return await self.documents()
            if deadline is not None and time.monotonic() > deadline:
                raise TimeoutError(
                    f"crawl {self.id} still {state!r} after {timeout}s "
                    f"({page.get('completed', 0)}/{page.get('total', 0)} pages)"
                )
            await asyncio.sleep(poll_interval)

    async def cancel(self) -> None:
        await self._transport.request("DELETE", f"/v1/crawl/{self.id}", retry=False)


class CrawlResource:
    def __init__(self, transport: Transport) -> None:
        self._transport = transport

    async def crawl(self, url: str, **options: Any) -> CrawlJob:
        """Queue a crawl. Returns immediately -- `await job.wait()` to block.

        `options` are `CrawlRequest` fields: `limit`, `include_paths`,
        `exclude_paths`, `max_discovery_depth`, `allow_subdomains`,
        `ignore_robots_txt`, `scrape_options`, `webhook`, and the rest.
        """

        payload = await self._transport.request(
            "POST", "/v1/crawl", json={"tenant": "-", "url": url, **options}
        )
        return CrawlJob(
            self._transport, payload["id"], webhook_secret=payload.get("webhook_secret")
        )

    async def job(self, job_id: str) -> CrawlJob:
        """A handle on a crawl queued earlier, by id."""

        return CrawlJob(self._transport, job_id)
