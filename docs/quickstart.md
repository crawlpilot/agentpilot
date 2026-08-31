# Quickstart

## Install

```bash
pip install "crawlpilot[engine]"   # the library plus a real browser driver
patchright install chrome          # the browser itself (arm64: install chromium)
```

`crawlpilot` drives a browser on your own machine. There is no API key and no
hosted service — the only account you need is the one on your laptop.

## Your first scrape

```python
from crawlpilot import Crawlpilot

with Crawlpilot() as cp:
    doc = cp.scrape("https://example.com")
    print(doc.markdown)
```

That is the whole thing. `Crawlpilot()` picks a browser, starts on a stealth
tier that escalates if something blocks it, and turns on block detection so a
CAPTCHA page comes back as an error rather than as markdown that looks like
content.

## Several pages

```python
with Crawlpilot() as cp:
    for doc in cp.batch_scrape(["https://a.test", "https://b.test"]):
        if doc.error:
            print(f"{doc.url}: {doc.error}")
        else:
            print(f"{doc.url}: {len(doc.markdown)} chars")
```

Results come back in the order you gave them, one `Document` per URL. A page
that fails carries `error` instead of raising — a fifty-URL run should not lose
forty-nine good results to one dead host.

## Sync or async

`Crawlpilot` is sync. `AsyncCrawlpilot` is the same object with `await` on each
call, for when you already have an event loop — a FastAPI handler, an agent
runtime, anything you would otherwise block:

```python
from crawlpilot import AsyncCrawlpilot

async with AsyncCrawlpilot() as cp:
    doc = await cp.scrape("https://example.com")
```

The sync client runs one event loop on one worker thread for its lifetime, not
one per call, so the browser stays warm between `scrape`s.

## Driving a page

When you need to interact rather than just read, open a session:

```python
with Crawlpilot() as cp:
    with cp.session() as page:
        page.navigate("https://example.com/login")
        page.fill("#email", "me@example.com")
        page.fill("#password", "hunter2")
        page.click("button[type=submit]")
        print(page.get_title())
```

See [interacting.md](interacting.md) for how elements are addressed, and
`examples/snapshot_and_click.py` for a runnable version.

## What comes back

`scrape()` returns a `Document`:

| Field | What it holds |
|---|---|
| `markdown` | the page as markdown (the default format) |
| `html`, `text` | other formats, when you ask for them via `formats=` |
| `structured_data` | JSON-LD, OpenGraph and hydration state, with `formats=("structured_data",)` |
| `error` | why this URL produced nothing — `None` on success |
| `metadata` | `title`, `status_code`, `tier_used`, `duration_ms` |

```python
doc = cp.scrape(url, formats=("markdown", "structured_data"))
print(doc.metadata.tier_used)   # which rung actually served it
```

## Configuration

Everything is optional and defaults to something that works:

```python
Crawlpilot(
    proxy="http://user:pass@gateway:8080",  # a URL, not a builder
    headful=True,                           # watch it run
    channel="chromium",                     # override browser choice
    extensions=[RetailExtension()],         # site-specific knowledge
)
```

See [anti-detection.md](anti-detection.md) for when each of those matters.

## The layer underneath

`Crawlpilot` is a facade over `Browser`, which takes an injectable driver,
registry, proxy pinner, prototype provider and egress policy. The platform uses
that; a crawler should not have to. `cp.session()` hands you the lower layer
already assembled, and `cp.browser` is the `Browser` itself if you need it.

## Not included

`crawl()` — following links across a site, with a frontier, robots handling,
deduplication and sitemaps — is not part of this library. It lives one layer up
in `agentpilot.crawl`, because it is a different problem (scheduling and state)
than driving a browser.
