# Driving a fleet

[`crawlpilot`](../packages/crawlpilot) drives a browser on your machine.
[`agentpilot-client`](../packages/agentpilot-client) drives one on an agentpilot
fleet. They are the same object.

```bash
pip install agentpilot-client
```

```python
from agentpilot_client import AgentPilot

with AgentPilot(api_key=KEY, base_url="https://gateway.example.com") as ap:
    print(ap.scrape("https://example.com").markdown)
```

`api_key` and `base_url` default to `AGENTPILOT_API_KEY` and `AGENTPILOT_URL`.
`AsyncAgentPilot` is the same object with `await` on each call.

## The same object

This is the property everything else is arranged around:

```python
def flow(cp):                                  # one body
    with cp.session() as page:
        page.navigate("https://example.com")
        page.click("#buy")
        return page.get_title(), page.is_visible(selector="#cart")

flow(Crawlpilot())                             # a browser here
flow(AgentPilot(api_key=KEY))                  # a browser on the fleet
```

Not a coincidence of naming. The ~60 verbs live once in
`crawlpilot.verbs.SessionVerbs`, over an abstract `execute()`. A local session
supplies `execute` by calling the driver; a remote one by POSTing a batch. CI
asserts the client *inherits* them rather than restating any.

The same holds for results and errors. `scrape()` returns
`crawlpilot.spi.scrape.Document` either way, and a server error arrives as the
exception you would have caught locally:

```python
from agentpilot_client import StaleRefError, CapacityExhausted

try:
    ...
except StaleRefError:        # re-snapshot and retry
    ...
except CapacityExhausted:    # the fleet is full
    ...
```

## Surface

```python
ap.scrape(url, formats=("markdown",), tier="auto", **options)  -> Document
ap.batch_scrape(urls, concurrency=5, ...)                      -> list[Document]
ap.map(url, limit=..., search=...)                             -> list[Link]
ap.crawl(url, limit=...)                                       -> CrawlJob
ap.session(domain=..., tier=...)                               -> RemoteSession
ap.agent.run(task, domain=...)                                 -> AgentRun
ap.recipes.{create,list,get,run,heal,codegen,versions}
ap.capabilities()                                              -> what this deployment can do
```

Jobs are queued and polled, with the loop written for you:

```python
job = ap.crawl("https://example.com", limit=50)
documents = job.wait()          # or job.status() to drive your own loop
```

## Two things the client deliberately does not take

**No `tenant`.** Every route derives it from the API key and overwrites whatever
the body said. An argument would imply you could act for a tenant that is not
yours.

**No `extensions=` on the constructor.** An extension is *code* — hooks that
rewrite a URL, warm a site up, resolve a wall — and code does not cross a
network. Site knowledge is installed into the worker image; you select among
what is there, by name, per call:

```python
ap.capabilities()["extensions"]                    # what this deployment loaded
ap.scrape(url, extensions=["retail"])              # run this scrape with just that one
ap.scrape(url, extensions=[])                      # ...or with none, to see the page raw
```

Extension-contributed verbs are callable even though this client's own catalog
has never heard of them — the session's registry is built from what the server
reported:

```python
with ap.session() as page:
    page.call_tool("walmart.solve_wall", {"reason": "captcha"})
    page.solve_wall(reason="captcha")               # same thing, resolved dynamically
```

## Version skew

`capabilities()` reports the wire version, checked against the client's on first
use. A server *older* than the client is fine — it simply lacks verbs you know
about. A server *newer* is refused up front, with both versions named, rather
than failing deep inside a later call:

```
IncompatibleServer: server at https://gw targets api 2.0, newer than the
host's 1.0; refusing rather than failing inside a hook later
```

Additive changes never break a client: response decoding ignores fields it does
not recognise, and fills in ones an older server omits.

## Two ways to drive a remote browser

| | wire (default) | CDP attach |
|---|---|---|
| install | `httpx` + crawlpilot base, **no browser** | needs `crawlpilot[engine]` + a real browser |
| round trips | one per *batch* of actions | one per call — chatty |
| server-side policy | tier ladder, egress guard, audit all apply | bypassed, raw CDP |
| stealth | the tier you asked for | weaker; the driver logs this |
| use it for | the default — anything scripted or at volume | debugging, Playwright-shaped scripts |

The escape hatch, when you want the full in-process object against a fleet
browser:

```python
with ap.session(enable_cdp=True) as page:
    with Crawlpilot(cdp_url=page.cdp_url(), cdp_headers=page.cdp_headers()) as cp:
        with cp.session() as local:
            local.navigate("https://example.com")   # runs on the fleet
```

`enable_cdp=True` is opt-in per session, precisely because it steps around the
policy the wire path applies. See
[`examples/remote_cdp_attach.py`](../examples/remote_cdp_attach.py) and
[`examples/remote_scrape.py`](../examples/remote_scrape.py).

## Testing without a gateway

`Transport` is injectable, the counterpart of `Browser`'s injectable driver, so
a consumer's own tests can run against `pytest-httpserver` rather than a live
fleet:

```python
from agentpilot_client import AgentPilot, Transport

ap = AgentPilot(transport=Transport(httpserver.url_for("/"), "test-key"))
```
