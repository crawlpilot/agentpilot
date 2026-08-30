# crawlpilot

An embeddable browser platform. Navigation, interaction, stealth tiers, proxy
and profile management, block detection, and HTML → Markdown extraction —
usable directly as a library, not only behind an HTTP service.

```python
from crawlpilot import Browser

async with Browser() as browser:
    async with browser.session() as page:
        await page.navigate("https://example.com")
        print(await page.markdown())
```

Everything is optional and defaults to something inert-but-working: a real
Chrome driver, an in-process session registry, no proxies, no extensions.

## Install

```bash
pip install "crawlpilot[engine,markdown]"   # real Chrome + markdown extraction
```

| Extra | For |
|---|---|
| *(base)* | HTTP fast-path tier and the content pipeline |
| `engine` | real Chrome (Patchright) |
| `markdown` | HTML → Markdown / structured data |
| `vault` | encryption-at-rest for saved profiles |

The base install carries no web framework, no database driver and no Redis.

## Two shapes

`browser.scrape(url)` is one-shot: it mints a throwaway identity, runs one
batch and tears the context down — right for a list of independent URLs.
`browser.session()` keeps a context alive across many calls — right when you
need to interact rather than only read.

## Extending it

crawlpilot ships no site-specific knowledge. A consumer contributes it as an
extension: hooks for URL rewriting, site warm-up, block classification, block
*resolution*, markup repair and document enrichment. See
`crawlpilot.extensions`.

`cp.tools` exposes every browser verb as a vendor-neutral `ToolSpec` (name,
description, JSON Schema) with optional adapters for Anthropic / OpenAI / MCP —
no LLM SDK is imported.
