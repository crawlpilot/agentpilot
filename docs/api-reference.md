# API reference

Every public method, its real return type, and what it is for.

- [`Crawlpilot` / `AsyncCrawlpilot`](#crawlpilot) — the one-call client
- [`BrowserSession`](#browsersession) — a live page
- [`Browser`](#browser) — the assembly layer
- [Errors](#errors)

---

## Crawlpilot

`Crawlpilot` is sync; `AsyncCrawlpilot` is identical with `await` on every call.

### Constructor

| Argument | Default | Meaning |
|---|---|---|
| `proxy` | `None` | Proxy URL, or a list of them. `"http://user:pass@host:8080"` |
| `proxy_country` | `None` | ISO country of the exit, used to keep the fingerprint's geo coherent |
| `headful` | `None` | `None` = if a display exists. Applies to `scrape()` too |
| `channel` | `None` | Playwright channel: `chrome`, `chromium`, `msedge`… |
| `executable_path` | `None` | A specific browser binary; wins over `channel` |
| `cdp_url` | `None` | Attach to a browser running elsewhere; nothing is launched |
| `profiles_root` | temp dir | Where identity profiles live. Must persist for warm identities |
| `extensions` | `()` | `BlockMount`/`BrowseMount`/… contributors |
| `detect_blocks` | `True` | Classify pages and wait for `_abck`. See [anti-detection](anti-detection.md) |

Any other keyword is passed through to `Browser`.

### Methods

| Method | Returns | Notes |
|---|---|---|
| `scrape(url, *, formats=("markdown",), tier="auto", **options)` | `Document` | One page, with the escalation ladder |
| `batch_scrape(urls, ...)` | `list[Document]` | In order; a failure carries `Document.error` rather than raising |
| `session(**kwargs)` | context manager → `BrowserSession` | The lower layer, assembled |
| `close()` | `None` | Also called by `__exit__` |

`options` are `ScrapeOptions` fields: `only_main_content`, `timeout_ms`,
`wait_for_ms`, `actions`, `screenshot`, `block_images`, `block_hosts`.

```python
with Crawlpilot(proxy="http://gw:8080") as cp:
    doc = cp.scrape("https://example.com", formats=("markdown", "structured_data"))
    print(doc.metadata.tier_used, len(doc.markdown))
```

### `proxy_endpoint(url, *, tier="residential", country=None)`

A `ProxyEndpoint` from a URL, if you are building a `ProxyPinner` yourself.

---

## BrowserSession

Yielded by `session()`. In a sync client the same methods exist without `await`.

### Navigation

| Method | Returns |
|---|---|
| `navigate(url, *, wait_until="load", timeout_ms=30000, referer=None)` | `ActionResult` |
| `go_back()` / `forward()` / `reload()` | `ActionResult` |

### Interaction

Each takes a **CSS selector positionally** or a **`ref=` keyword** — exactly one.
See [interacting.md](interacting.md).

| Method | Returns |
|---|---|
| `click(selector=None, *, ref=None, all=False)` | `ActionResult` |
| `fill(selector=None, text="", *, ref=None, clear=True)` | `ActionResult` |
| `hover` / `double_click` / `focus` / `tap` | `ActionResult` |
| `check` / `uncheck` / `clear` / `scroll_into_view` | `ActionResult` |
| `select_option(ref, *values)` | `ActionResult` |
| `press(key)` / `send_keys(keys)` / `insert_text(text)` | `ActionResult` |
| `scroll(direction="down", *, pages=1.0, ref=None)` | `ActionResult` |
| `drag(ref, to_ref)` / `swipe(direction, ...)` | `ActionResult` |
| `upload_file(ref, path)` | `ActionResult` |
| `wait(ms)` / `find_text(text)` | `ActionResult` |

### Reading — all return real values

| Method | Returns | `None` when |
|---|---|---|
| `get_title()` | `str` | |
| `get_url()` | `str` | |
| `get_text(...)` / `get_html(...)` / `get_value(...)` | `str` | |
| `get_attribute(name, ...)` | `str \| None` | the attribute is absent |
| `get_count(selector)` | `int` | |
| `get_box(...)` | `dict[str, float] \| None` | the element has no geometry |
| `get_styles(..., properties=[...])` | `dict[str, str]` | |
| `is_visible(...)` / `is_enabled(...)` | `bool` | |
| `is_checked(...)` | `bool \| None` | the checkbox is indeterminate |
| `dropdown_options(ref)` | `list[dict[str, str]]` | |
| `search_page(pattern, *, regex=False)` | `list[str]` | |
| `find_elements(selector, *, attributes=())` | `list[dict]` | |

### Content

| Method | Returns |
|---|---|
| `markdown(*, main_content=True)` / `text(...)` / `html()` | `str` |
| `extract(fmt="markdown", *, main_content=True)` | `str` |
| `screenshot(*, full_page=False)` | `bytes` |
| `pdf(*, landscape=False, scale=1.0)` | `bytes` |
| `snapshot(*, settle=True)` | `EnhancedDOMTreeNode \| None` |
| `diff_snapshot(*, settle=False)` | `str` |
| `execute_js(script)` | `Any` |

### Waiting — each raises `WaitTimeout`

`wait_for_selector(selector, *, state="visible", timeout_ms=10000)`,
`wait_for_text(text, ...)`, `wait_for_url(url, ...)`,
`wait_for_load(state="load", ...)`.

### Tabs, frames, dialogs

`new_tab(url=None)`, `switch_tab(page_id)`, `close_tab(page_id)`,
`list_tabs() -> list[TabInfo]`, `list_frames() -> list[FrameInfo]`,
`dialog_status() -> DialogInfo | None`, `dialog_accept(...)`,
`dialog_dismiss()`, `download(ref, ...)`.

### `execute(actions, *, page_id=None) -> ActionResult`

The primitive everything above is sugar over. One batch, one round trip.

`ActionResult` fields are per-type and index-correlated: `extracts`,
`screenshots`, `pdfs`, `downloads`, `fused_trees`, `tabs`, `frames`,
`js_returns`, `verifications`, **`readouts`** (prose, for an agent),
**`values`** (the same answers as values), `page_title`, `status_code`,
`soft_verdict`, `dialog`.

---

## Browser

The assembly layer. Use it directly when you need to inject a component — the
platform passes a Redis registry, a tenant-aware proxy provider and an egress
policy. `Crawlpilot` is a facade over exactly this.

`Browser(config=None, *, driver, registry, proxy_pinner, prototype_provider,
extensions, profiles_root, lease_ttl_seconds, egress, executable_path, channel,
headful, cdp_url)`

| Method | Returns |
|---|---|
| `scrape(url, *, formats, tier, identity, options)` | `Document` |
| `session(*, identity, domain, tier, headful, detect_blocks, locale, timezone_id, dialogs)` | context manager → `BrowserSession` |
| `Browser.from_system_chrome(**kwargs)` | `Browser` pinned to installed Chrome |

---

## Errors

All subclass `DriverError`.

| Exception | Raised when |
|---|---|
| `SelectorNotFound` | a CSS selector matched nothing |
| `StaleRefError` | the ref's snapshot was superseded, or the node is gone |
| `WaitTimeout` | a `wait_for_*` condition never came true |
| `NavigationTimeout` | the page never reached the requested load state |
| `ChallengeDetected` | a hard bot wall; carries `verdict`, `weight`, `scope` |
| `ContextCrashed` | the browser context died |

A dead host raises the underlying automation library's own error, not one of
these — which is why `batch_scrape` catches broadly and records the failure on
`Document.error`.
