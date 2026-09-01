# agentpilot-client

An [agentpilot](../agentpilot) fleet, driven as the same object as a local
browser.

```python
from agentpilot_client import AgentPilot

with AgentPilot(api_key=KEY) as ap:
    print(ap.scrape("https://example.com").markdown)
```

No browser is installed and none is launched — the browsing happens on the
fleet.

## The point

A session from this client and a session from a local
[`crawlpilot`](../crawlpilot) browser are the *same class* underneath, so the
same code drives either:

```python
def flow(cp):
    with cp.session() as page:
        page.navigate("https://example.com")
        page.click("#buy")
        return page.get_title(), page.is_visible("#cart")

flow(Crawlpilot())                 # a browser on this machine
flow(AgentPilot(api_key=KEY))      # a browser on the fleet
```

That is not a coincidence of API design. The ~60 browser verbs live once, in
`crawlpilot.verbs.SessionVerbs`, over an abstract `execute()`. A local session
supplies `execute` by calling the driver; `RemoteSession` supplies it by POSTing
to `/v1/sessions/{id}/execute`. Neither restates a verb, and adding one to
`crawlpilot/tools/catalog.py` reaches both.

## Install

```bash
pip install agentpilot-client
```

Two dependencies: `httpx` and `crawlpilot` (the base install — no browser, no
Patchright). No FastAPI, no Redis, no Postgres. CI installs this package alone
and fails if a server dependency appears in its closure.

## What comes from where

| | |
|---|---|
| the verbs | `crawlpilot.verbs.SessionVerbs` |
| action models | `crawlpilot.tools.CATALOG` — the same ones the gateway validates against |
| result decoding | `crawlpilot.wire.from_wire` |
| `Document`, `ScrapeOptions` | `crawlpilot.spi.scrape` |
| exceptions | `crawlpilot.spi.errors` |

This package adds transport, auth, job polling and ergonomics. It defines no
result type and no verb of its own, which is why it cannot drift from the server.

## Surface

```python
ap = AgentPilot(api_key=..., base_url=..., timeout=..., transport=...)

ap.scrape(url, formats=("markdown",), tier="auto", **options)  -> Document
ap.batch_scrape(urls, concurrency=5, ...)                      -> list[Document]
ap.map(url, limit=..., search=...)                             -> list[Link]
ap.crawl(url, limit=...)                                       -> CrawlJob  (.wait(), .cancel())
ap.session(domain=..., tier=...)                               -> RemoteSession (context manager)
ap.agent.run(task, domain=...)                                 -> AgentRun  (.wait(), .steps())
ap.recipes.{create,list,get,run,heal,codegen,versions}
ap.capabilities()                                              -> wire version, verbs, extensions
```

`AsyncAgentPilot` is the same object with `await` on each call.

There is **no `tenant` argument** — every route derives it from the API key, so
offering one would imply you could act for a tenant that is not yours. There is
**no `extensions=` constructor argument** either: an extension is *code* and
cannot cross a network, so site knowledge is installed into the worker image and
selected by name per call (`scrape(extensions=[...])`). `capabilities()` lists
what is installed.

## Errors

Server errors arrive as the exceptions you would catch locally — the codes are
declared on the classes in `crawlpilot.spi.errors`, so neither side keeps a
mapping table:

```python
from agentpilot_client import StaleRefError, CapacityExhausted

try:
    ...
except StaleRefError:
    ...          # re-snapshot and retry
except CapacityExhausted:
    ...          # the fleet is full; Retry-After was already honoured twice
```

`Retry-After` is respected automatically on the two statuses that set it (a
contended lease, a full fleet). Nothing else is retried — a browser action that
may have already run is not safe to repeat.

## Two ways to drive a remote browser

This client batches actions over HTTP: one round trip per `execute`, with the
server's tier ladder, egress guard and audit all applying. That is the default
and what you want at volume.

For interactive debugging, `session.cdp_url()` hands you a Chrome-shaped
endpoint you can point a *local* browser object at:

```python
with ap.session(enable_cdp=True) as page:
    with Crawlpilot(cdp_url=page.cdp_url(), cdp_headers=page.cdp_headers()) as cp:
        ...   # full in-process object, remote browser
```

That needs a real browser driver locally, costs a round trip per call rather
than per batch, and bypasses the server-side policy. See
[`docs/client.md`](../../docs/client.md).
