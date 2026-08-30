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
pip install "crawlpilot[engine]"   # + a real browser
```

| Extra | For |
|---|---|
| *(base)* | HTTP fast-path tier and the content pipeline (HTML → Markdown / structured data) |
| `engine` | driving a real browser (Patchright) |
| `vault` | encryption-at-rest for saved profiles |

The base install carries no web framework, no database driver and no Redis.

### A browser

`crawlpilot[engine]` installs the automation library, not a browser. It finds one
in this order:

1. `Browser(executable_path=...)`, if you pass one
2. `Browser(channel="chrome" | "chromium" | "msedge" | ...)`
3. Google Chrome, wherever your OS installs it
4. a Chromium previously fetched by `patchright install chromium`

If none of those turn anything up you get an error naming the install command.
To pin the browser explicitly:

```bash
patchright install chrome      # real Google Chrome (x86_64 only)
patchright install chromium    # bundled Chromium (also works on arm64)
```

To drive a browser running somewhere else — another container, another host —
pass its CDP endpoint instead and nothing is launched locally:

```python
Browser(cdp_url="http://chrome:9222")
```

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
