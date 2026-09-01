"""`POST /v1/scrape` -- one page, composed server-side.

Returns `crawlpilot.spi.scrape.Document`, the same type a local
`Crawlpilot().scrape()` returns, so a caller reading `.markdown` or
`.metadata.tier_used` cannot tell which one produced it. That is the point:
this package defines no result type of its own.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from crawlpilot.spi.scrape import Document, DocumentMetadata

if TYPE_CHECKING:
    from agentpilot_client._transport import Transport

DEFAULT_TIER = "auto"
"""Same default as the local client, and for the same reason: `auto` fetches
over plain HTTP first and climbs to a browser only when something blocks, so it
is both the cheapest and the most robust choice -- not a decision a caller
should have to make."""


class ScrapeResource:
    def __init__(self, transport: Transport) -> None:
        self._transport = transport

    async def scrape(
        self,
        url: str,
        *,
        formats: Sequence[str] = ("markdown",),
        tier: str = DEFAULT_TIER,
        extensions: Sequence[str] | None = None,
        **options: Any,
    ) -> Document:
        """One page.

        `options` are `ScrapeOptions` fields -- `only_main_content`,
        `timeout_ms`, `wait_for_ms`, `screenshot`, `block_images`, `locale`,
        `session_name` and the rest. Passed through rather than enumerated, so a
        field added to the server's schema is usable here immediately.

        No `tenant` argument, deliberately: every `/v1` route overwrites it from
        the authenticated key, and offering one would imply a caller could act
        for a tenant that is not theirs.
        """

        body: dict[str, Any] = {
            "tenant": _PLACEHOLDER,
            "url": url,
            "formats": list(formats),
            "tier": tier,
            **options,
        }
        if extensions is not None:
            body["extensions"] = list(extensions)
        payload = await self._transport.request("POST", "/v1/scrape", json=body)
        return document_from_wire(payload["data"])

    async def batch(
        self, urls: Sequence[str], *, concurrency: int = 5, **kwargs: Any
    ) -> list[Document]:
        """Several pages, in the order given.

        A failure comes back as a `Document` carrying `error` rather than
        raising, matching the local client: a fifty-URL run should not lose
        forty-nine good results to one dead host.
        """

        semaphore = asyncio.Semaphore(concurrency)

        async def one(url: str) -> Document:
            async with semaphore:
                try:
                    return await self.scrape(url, **kwargs)
                except Exception as exc:  # noqa: BLE001 -- carried, not swallowed
                    return Document(document_id="", url=url, error=str(exc))

        return list(await asyncio.gather(*(one(url) for url in urls)))


_PLACEHOLDER = "-"


def document_from_wire(payload: dict[str, Any]) -> Document:
    """`DocumentOut` -> `Document`.

    Field names match on both sides (the wire model is a mirror of the
    dataclass), so this copies rather than translates. `screenshot` is the one
    exception: it travels base64 and inline, while the dataclass carries an
    artifact id for the job-backed paths that persist one.
    """

    metadata = payload.get("metadata")
    return Document(
        document_id=payload.get("document_id", ""),
        url=payload.get("url", ""),
        markdown=payload.get("markdown"),
        text=payload.get("text"),
        html=payload.get("html"),
        structured_data=payload.get("structured_data"),
        links=tuple(payload.get("links") or ()),
        screenshot_artifact_id=payload.get("screenshot"),
        metadata=(
            DocumentMetadata(
                title=metadata.get("title"),
                status_code=metadata.get("status_code"),
                tier_used=metadata.get("tier_used", ""),
                node_id=metadata.get("node_id", ""),
                duration_ms=metadata.get("duration_ms", 0.0),
                source_url=metadata.get("source_url", payload.get("url", "")),
            )
            if metadata
            else None
        ),
        error=payload.get("error"),
        extract=payload.get("extract"),
        extract_error=payload.get("extract_error"),
        extract_warning=payload.get("extract_warning"),
    )
