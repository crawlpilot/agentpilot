"""`AgentPilot` and `AsyncAgentPilot` -- the one-call entry point.

Deliberately the same two layers as `crawlpilot.client`, because it is the same
problem: `Transport` is the assembly layer with every part injectable, and this
is that with the decisions already made.

    with AgentPilot(api_key=KEY) as ap:
        print(ap.scrape("https://example.com").markdown)

**Sync is the headline**, matching `Crawlpilot`. `AsyncAgentPilot` holds the
logic; `AgentPilot` runs it on a private event loop in a worker thread -- not
`asyncio.run` per call, which would drop the pooled connection between calls.
That machinery is `crawlpilot._sync`, shared with the local client rather than
written a second time.

The verbs on a session are not defined here at all. `RemoteSession` inherits
them from `crawlpilot.verbs.SessionVerbs`, so this file is transport, polling
and ergonomics -- and a verb added to `tools/catalog.py` arrives without an edit.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import asynccontextmanager, contextmanager
from typing import Any

import httpx

from agentpilot_client._transport import DEFAULT_TIMEOUT, Transport
from agentpilot_client.resources import (
    AgentResource,
    AgentRun,
    CrawlJob,
    CrawlResource,
    Link,
    MapResource,
    RecipeResource,
    ScrapeResource,
)
from agentpilot_client.session import RemoteSession
from crawlpilot._sync import LoopThread, SyncProxy
from crawlpilot.spi.scrape import Document
from crawlpilot.tools import ToolRegistry, ToolSpec, browser_tools


class AsyncAgentPilot:
    """The async client. `AgentPilot` is this, without the `await`."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        transport: Transport | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        """`api_key` and `base_url` fall back to `AGENTPILOT_API_KEY` and
        `AGENTPILOT_URL`, so a script that reads its config from the environment
        needs no arguments at all.

        There is no `tenant` argument. Every `/v1` route derives the tenant from
        the authenticated key and overwrites whatever the body said, so offering
        one would imply a caller could act for a tenant that is not theirs.

        There is no `extensions` argument either. An `Extension` is *code* and
        cannot cross a network -- site knowledge reaches a worker as a
        `pip install` into its image. Select among what is installed there by
        name, per call (`scrape(extensions=[...])`), and see what exists with
        `capabilities()`.

        `transport` is the injectable seam, the counterpart of `Browser`'s
        injectable driver: it is what lets a consumer test against a fake
        gateway without running one.
        """

        if transport is None:
            resolved_key = api_key or os.environ.get("AGENTPILOT_API_KEY", "")
            resolved_url = base_url or os.environ.get("AGENTPILOT_URL", "http://localhost:8000")
            if not resolved_key:
                raise ValueError(
                    "an api key is required: pass api_key=... or set AGENTPILOT_API_KEY"
                )
            transport = Transport(
                resolved_url, resolved_key, timeout=timeout, client=http_client
            )

        self.transport = transport
        self._tools: ToolRegistry | None = None

        self._scrape = ScrapeResource(transport)
        self._map = MapResource(transport)
        self._crawl = CrawlResource(transport)
        self.agent = AgentResource(transport)
        self.recipes = RecipeResource(transport)

    async def __aenter__(self) -> AsyncAgentPilot:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self.transport.aclose()

    # -------------------------------------------------------- capabilities

    async def capabilities(self) -> dict[str, Any]:
        """What this deployment can do: wire version, verbs, extensions.

        Fetched once and cached. The wire version is checked against this
        client's on the first call, so a server too new to understand fails
        immediately with both versions named rather than deep inside a request.
        """

        return await self.transport.capabilities()

    async def extensions(self) -> list[dict[str, Any]]:
        """The extensions this deployment loaded -- the names `scrape(extensions=[...])`
        accepts."""

        return list((await self.capabilities()).get("extensions", []))

    async def tools(self) -> ToolRegistry:
        """The verbs the *server* can dispatch, which is a superset of this
        build's `CATALOG` whenever an extension contributed one.

        A verb the server has that this client does not is registered as a
        placeholder: enough for `call_tool` to send it and for it to show up in
        the registry, without pretending we know its argument types.
        """

        if self._tools is None:
            registry = browser_tools()
            known = set(registry.names)
            for tool in (await self.capabilities()).get("tools", []):
                name = tool.get("name", "")
                if name in known or "." not in name:
                    continue
                namespace, bare = name.split(".", 1)
                registry.register(_placeholder_spec(bare, tool), namespace=namespace)
            self._tools = registry
        return self._tools

    # --------------------------------------------------------------- scrape

    async def scrape(self, url: str, **kwargs: Any) -> Document:
        return await self._scrape.scrape(url, **kwargs)

    async def batch_scrape(self, urls: Sequence[str], **kwargs: Any) -> list[Document]:
        return await self._scrape.batch(urls, **kwargs)

    async def map(self, url: str, **kwargs: Any) -> list[Link]:
        return await self._map.map(url, **kwargs)

    async def crawl(self, url: str, **kwargs: Any) -> CrawlJob:
        return await self._crawl.crawl(url, **kwargs)

    async def crawl_job(self, job_id: str) -> CrawlJob:
        return await self._crawl.job(job_id)

    # -------------------------------------------------------------- session

    @asynccontextmanager
    async def session(
        self,
        *,
        domain: str = "",
        name: str = "default",
        tier: str = "auto",
        enable_cdp: bool = False,
        **options: Any,
    ) -> AsyncIterator[RemoteSession]:
        """An open session on the fleet, released when the block exits.

        The object yielded has the same ~60 verbs as a local
        `Crawlpilot().session()`, because it is the same class underneath
        (`crawlpilot.verbs.SessionVerbs`).

        `enable_cdp=True` additionally makes `page.cdp_url()` usable -- opt-in
        per session, because a raw CDP relay bypasses the batching and the
        server-side policy the wire path applies.
        """

        body = {
            "tenant": "-",
            "domain": domain,
            "name": name,
            "tier": tier,
            "enable_cdp": enable_cdp,
            **options,
        }
        payload = await self.transport.request("POST", "/v1/sessions", json=body, retry=True)
        session = RemoteSession(
            self.transport,
            payload["session_id"],
            metadata=payload.get("metadata"),
            tools=await self.tools(),
        )
        try:
            yield session
        finally:
            await session.close()


class AgentPilot:
    """The sync client. Same methods as `AsyncAgentPilot`, without the `await`.

        with AgentPilot(api_key=KEY) as ap:
            print(ap.scrape("https://example.com").markdown)

    One event loop on one worker thread for the client's life, rather than
    `asyncio.run` per call -- the pooled HTTPS connection to the gateway is the
    expensive part, and a loop per call would drop it between requests.
    """

    def __init__(self, **kwargs: Any) -> None:
        self._loop = LoopThread(name="agentpilot-client")
        self._async = AsyncAgentPilot(**kwargs)
        self.agent = SyncProxy(self._async.agent, self._call)
        self.recipes = SyncProxy(self._async.recipes, self._call)

    def _call(self, coro: Any) -> Any:
        return self._loop.call(coro)

    def __enter__(self) -> AgentPilot:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._loop.closed:
            return
        self._call(self._async.close())
        self._loop.close()

    # The async client owns the behaviour; these only cross the thread boundary.

    def capabilities(self) -> dict[str, Any]:
        return self._call(self._async.capabilities())  # type: ignore[no-any-return]

    def extensions(self) -> list[dict[str, Any]]:
        return self._call(self._async.extensions())  # type: ignore[no-any-return]

    def scrape(self, url: str, **kwargs: Any) -> Document:
        return self._call(self._async.scrape(url, **kwargs))  # type: ignore[no-any-return]

    def batch_scrape(self, urls: Sequence[str], **kwargs: Any) -> list[Document]:
        return self._call(self._async.batch_scrape(urls, **kwargs))  # type: ignore[no-any-return]

    def map(self, url: str, **kwargs: Any) -> list[Link]:
        return self._call(self._async.map(url, **kwargs))  # type: ignore[no-any-return]

    def crawl(self, url: str, **kwargs: Any) -> Any:
        return SyncProxy(self._call(self._async.crawl(url, **kwargs)), self._call)

    @contextmanager
    def session(self, **kwargs: Any) -> Iterator[Any]:
        """A live session, driven synchronously.

        Entered and exited on the client's loop, so the remote session lives
        exactly as long as the `with` block. The yielded object forwards every
        verb through `SyncProxy`, so it needs no per-method wrapper and cannot
        fall out of step with `SessionVerbs`.
        """

        ctx = self._async.session(**kwargs)
        page = self._call(ctx.__aenter__())
        try:
            yield SyncProxy(page, self._call)
        finally:
            self._call(ctx.__aexit__(None, None, None))


def _placeholder_spec(name: str, tool: dict[str, Any]) -> ToolSpec:
    """A stand-in for a server verb this build has no dataclass for.

    Deliberately thin. We know the name and the description the server gave, and
    nothing about the argument types -- so the spec exists to make the verb
    *callable* (`call_tool`, and the `__getattr__` fallback), and the server
    validates the arguments against its own real spec on arrival. Pretending to
    a richer local schema than we have would only produce confident client-side
    rejections of requests the server would have accepted.
    """

    import dataclasses

    action_cls = dataclasses.make_dataclass(f"{name.title().replace('_', '')}Action", [])
    return ToolSpec(
        name=name,
        description=tool.get("description", ""),
        action_cls=action_cls,
        wire_fields=(),
        agent_fields=(),
        safety=tool.get("safety", "safe"),
        domains=tuple(tool["domains"]) if tool.get("domains") else None,
    )


__all__ = [
    "AgentPilot",
    "AgentRun",
    "AsyncAgentPilot",
    "CrawlJob",
    "Link",
    "RemoteSession",
    "Transport",
]
