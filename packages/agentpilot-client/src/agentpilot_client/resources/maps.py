"""`POST /v1/map` -- fast URL discovery, synchronous, no job queue.

Named `maps` rather than `map` so the module does not shadow the builtin inside
this package; the method a caller types is still `ap.map(url)`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agentpilot_client._transport import Transport


@dataclass(frozen=True)
class Link:
    """One discovered URL, with whatever the sitemap or page gave for it."""

    url: str
    title: str | None = None
    description: str | None = None


class MapResource:
    def __init__(self, transport: Transport) -> None:
        self._transport = transport

    async def map(self, url: str, **options: Any) -> list[Link]:
        """Every URL discoverable from `url`.

        `options` are `MapRequest` fields -- `limit`, `search`,
        `include_paths`, `exclude_paths`, `sitemap`, `include_subdomains`,
        `max_discovery_depth`, `timeout`.

        A partial result carries a `warning` on the wire; it is dropped here
        because the links are what a caller asked for and a truncated list is
        still useful. Reach for `map_with_warning` when you need to know.
        """

        links, _ = await self.map_with_warning(url, **options)
        return links

    async def map_with_warning(
        self, url: str, **options: Any
    ) -> tuple[list[Link], str | None]:
        payload = await self._transport.request(
            "POST", "/v1/map", json={"tenant": "-", "url": url, **options}
        )
        links = [
            Link(
                url=item["url"],
                title=item.get("title"),
                description=item.get("description"),
            )
            for item in payload.get("links", [])
        ]
        return links, payload.get("warning")
