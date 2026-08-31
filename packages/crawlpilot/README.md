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

## Examples

Runnable scripts live in [`examples/`](../../examples) — in the repo checkout,
not in the wheel. From the repo root:

```bash
uv sync --group dev --extra driver     # pulls crawlpilot[all]
uv run patchright install chrome       # one-time (arm64: install chromium instead)

uv run python examples/crawl_to_markdown.py https://example.com
uv run python examples/walmart_product_markdown.py
```

| Script | What it shows |
|---|---|
| `crawl_to_markdown.py` | both shapes side by side, on unprotected pages |
| `walmart_product_markdown.py` | the escalation ladder and block detection, against a PerimeterX-protected retail page |

Two env vars the Walmart one honours: `CRAWLPILOT_PROXY_URL` to route through a
residential exit, and `CRAWLPILOT_BROWSER_CHANNEL=chromium` to override the
real-Chrome default (required on arm64).

Both launch a real Chrome and hit the live sites named in them.

The Walmart one is where the platform earns its keep. It shows both shapes
against a target that fights back: `scrape(tier="auto")`, which climbs the
escalation ladder with a fresh identity, proxy and fingerprint per rung, and a
`session(detect_blocks=True)` for when you need to drive the page instead.

Three things it demonstrates that a naive script gets wrong:

- **`headful=True`** — a real window, and the OS-level input path the driver
  only has with a display. Not cosmetic: running headless is what got this
  script served a *"Robot or human?"* wall on every attempt. It is a preference
  rather than an assertion, so on a display-less box the driver logs a downgrade
  and runs headless instead of failing to launch.
- **`detect_blocks=True`** on the session. Off — the default, and the right one
  for agent runs — the warm-up skips its `_abck` wait and no page is ever
  classified, so a CAPTCHA interstitial is extracted and returned as if it were
  the product page.
- **`RetailExtension`** installed, contributing Walmart's own block signals
  (a landed `/blocked` URL, a `/ip/` page under 300 KB) through the ordinary
  `BlockMount` seam — the same path a third-party package would use.

Pass another Walmart URL as an argument to point it elsewhere.

## Extending it

crawlpilot ships no site-specific knowledge. A consumer contributes it as an
extension: hooks for URL rewriting, site warm-up, block classification, block
*resolution*, markup repair and document enrichment. See
`crawlpilot.extensions`.

`cp.tools` exposes every browser verb as a vendor-neutral `ToolSpec` (name,
description, JSON Schema) with optional adapters for Anthropic / OpenAI / MCP —
no LLM SDK is imported.
