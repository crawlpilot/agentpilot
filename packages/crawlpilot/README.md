# crawlpilot

An embeddable browser platform. Navigation, interaction, stealth tiers, proxy
and profile management, block detection, and HTML → Markdown extraction —
usable directly as a library, not only behind an HTTP service.

```python
from crawlpilot import Crawlpilot

with Crawlpilot() as cp:
    print(cp.scrape("https://example.com").markdown)
```

No API key, no hosted service: it drives a browser on your own machine.

`AsyncCrawlpilot` is the same object with `await` on each call, for when you
already have an event loop.

## Install

```bash
pip install "crawlpilot[engine]"   # + a real browser
patchright install chrome          # arm64: install chromium instead
```

| Extra | For |
|---|---|
| *(base)* | HTTP fast-path tier and the content pipeline (HTML → Markdown / structured data) |
| `engine` | driving a real browser (Patchright) |
| `vault` | encryption-at-rest for saved profiles |

The base install carries no web framework, no database driver and no Redis.

### A browser

`crawlpilot[engine]` installs the automation library, not a browser. It finds
one in this order:

1. `Crawlpilot(executable_path=...)`, if you pass one
2. `Crawlpilot(channel="chrome" | "chromium" | "msedge" | ...)`
3. Google Chrome, wherever your OS installs it
4. a Chromium previously fetched by `patchright install chromium`

If none turn anything up you get an error naming the install command. To drive a
browser running somewhere else, pass its CDP endpoint and nothing is launched
locally: `Crawlpilot(cdp_url="http://chrome:9222")`.

## Documentation

| | |
|---|---|
| [Quickstart](../../docs/quickstart.md) | install, first scrape, sync vs async |
| [Interacting](../../docs/interacting.md) | selectors vs refs, snapshots, batching, waits |
| [API reference](../../docs/api-reference.md) | every method and its real return type |
| [Anti-detection](../../docs/anti-detection.md) | tiers, proxies, headful, block detection |
| [Migrating to 0.2](../../docs/migration-0.2.md) | the breaking changes, with a before/after table |

## Two layers

**`Crawlpilot`** is the 90% case: `scrape`, `batch_scrape`, `session`, with
defaults chosen so the naive call is the correct one — an escalating stealth
tier, block detection on, headful where a display exists.

**`Browser`** underneath it takes an injectable driver, registry, proxy pinner,
prototype provider and egress policy. That is what the platform passes; a
crawler should not have to. `cp.session()` hands you the lower layer already
assembled, and `cp.browser` is the `Browser` itself.

`scrape()` is one-shot — it mints a throwaway identity, runs one batch and tears
the context down, which is right for a list of independent URLs. `session()`
keeps a context alive across many calls, for when you need to interact rather
than only read.

## Examples

Runnable scripts in [`examples/`](../../examples) — in the repo checkout, not
the wheel. From the repo root:

```bash
uv sync --group dev --extra driver
uv run patchright install chrome

uv run python examples/quickstart.py             # the three-liner
uv run python examples/snapshot_and_click.py     # snapshot, refs, clicking, typed getters
uv run python examples/crawl_to_markdown.py      # the async shape
uv run python examples/walmart_product_markdown.py   # a target that fights back
uv run python examples/remote_cdp_attach.py      # drive a fleet browser locally (needs a gateway)
```

`snapshot_and_click.py` runs offline against a fixture it writes itself. The
Walmart one hits the live site and honours `CRAWLPILOT_PROXY_URL` and
`CRAWLPILOT_BROWSER_CHANNEL`.

## Extending it

crawlpilot ships no site-specific knowledge. A consumer contributes it as an
extension: hooks for URL rewriting, site warm-up, block classification, block
*resolution*, markup repair and document enrichment. See `crawlpilot.extensions`,
and `agentpilot.control.retail_extension` as the reference implementation.

`crawlpilot.tools` exposes every browser verb as a vendor-neutral `ToolSpec`
(name, description, JSON Schema) with optional adapters for Anthropic / OpenAI /
MCP — no LLM SDK is imported.
