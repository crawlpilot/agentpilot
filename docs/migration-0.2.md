# Migrating to 0.2

Three breaking changes. The first one is a silent bug in existing code, so read
that section even if you skip the rest.

---

## 1. Getters return values, not sentences

Every getter used to return `ActionResult.readouts[0]` — prose written for an
LLM's prompt. They now return the value.

| Call | 0.1 returned | 0.2 returns |
|---|---|---|
| `get_title()` | `"title: 'Widget Shop'"` | `"Widget Shop"` |
| `get_url()` | `"url: https://x.test/a"` | `"https://x.test/a"` |
| `get_text(selector="#x")` | `"text of '#x': 'hello'"` | `"hello"` |
| `get_count(".item")` | `"count: 3 element(s) match '.item'"` | `3` |
| `get_attribute("href", ...)` | `"href of '#x': '/a'"` | `"/a"` (or `None`) |
| `get_box(...)` | `"box of '#x': 10,20 100x40"` | `{"x": 10.0, ...}` (or `None`) |
| `get_styles(...)` | `"styles of '#x': color='red'"` | `{"color": "red"}` |
| `is_visible(...)` | `"#x is visible"` / `"#x is not visible"` | `True` / `False` |
| `is_enabled(...)` | `"#x is enabled"` / `"#x is disabled"` | `True` / `False` |
| `is_checked(...)` | `"#x checked: true"` | `True` / `False` / `None` |
| `dropdown_options(ref)` | one formatted block | `list[dict]` |
| `search_page(pattern)` | one formatted block | `list[str]` |
| `find_elements(selector)` | one formatted block | `list[dict]` |

### The one that was a bug

```python
if await page.is_visible(selector="#cookie-banner"):
    await page.click("#dismiss")
```

In 0.1 this took the "visible" branch **always** — `"#cookie-banner is not
visible"` is a non-empty string, and therefore truthy. The code read correctly
and did the wrong thing, in silence. In 0.2 it does what it says.

Search your code for `is_visible`, `is_enabled` and `is_checked` used in a
boolean context. Those are the call sites that were wrong.

### If you wanted the prose

It still exists, and it is still what an agent should read:

```python
result = await page.execute([spi_actions.GetTitleAction()])
result.readouts[0]   # "title: 'Widget Shop'"   <- unchanged
result.values[0]     # "Widget Shop"            <- new
```

Nothing about the agent tool schema changed. `tools/catalog.py` produces the
same wire and agent JSON schemas it did before, byte for byte.

---

## 2. Interactions take a CSS selector positionally

`click`, `fill`, `hover`, `double_click`, `focus`, `check`, `uncheck`,
`scroll_into_view`, `clear` and `tap` now accept a CSS selector as well as a
ref — and the **positional** argument is the selector.

```python
# 0.1
await page.click("e42")
await page.fill("e17", "hello")

# 0.2
await page.click(ref="e42")             # a ref is now a keyword
await page.fill("#search", "hello")     # positional is CSS
await page.click("#add-to-cart")        # the new easy path
```

**Why not just detect it?** Because it cannot be detected. A ref is `e<index>`,
and `e42` is a valid CSS type selector matching an `<e42>` element. Any rule
based on the string would be a guess, and a guess here clicks the wrong element
silently.

**How this fails if you miss it:** `page.click("e42")` now looks for an `<e42>`
element and raises `SelectorNotFound`. Loud, not a mis-click.

Passing both raises `ValueError`.

Selectors have real limits — no cross-origin iframes, no shadow DOM, first match
on an ambiguous selector. See [interacting.md](interacting.md).

---

## 3. `Browser(headless=)` is now `Browser(headful=)`

```python
Browser(headless=False)   # 0.1
Browser(headful=True)     # 0.2
```

Same tri-state, positive sense, and the same word as `session(headful=...)`
— which it overrides.

The old pair was a genuine trap. `scrape()` ignores the session flag entirely
and derives headful from the tier rung it is on, so an `auto` scrape ran its
first rung headless no matter what the session asked for, and the only way to
get a window was the *other*, oppositely-named argument. One name now, one
meaning, everywhere.

`None` still means "headful if a display exists", and even `True` degrades to
headless on a machine with no display rather than failing to launch.

---

## 4. Exports recalibrated

**Added** to `crawlpilot`'s top level: `Crawlpilot`, `AsyncCrawlpilot`,
`SyncSession`, `proxy_endpoint`, `ProxyPinner`, `ProxyEndpoint`,
`SelectorNotFound`, `StaleRefError`, `WaitTimeout`.

**Removed** from the top level (still importable from `crawlpilot.extensions`):
`BlockHooks`, `BlockMount`, `BrowseHooks`, `BrowseMount`, `ContentHooks`,
`ContentMount`, `ToolMount`.

The principle: the top level carries what you import to *use* the library. The
mount protocols are what you import to *write an extension*, and they were
exported while `ProxyPinner` — which you need to route through a proxy — was
not.

---

## What did not change

- `Browser` and `BrowserSession` still exist with the same responsibilities.
  The new `Crawlpilot` client is a facade over them, not a replacement.
- `execute()` and the `Action` set are unchanged apart from the added
  `selector` fields.
- The agent tool schemas are unchanged.
- `ActionResult.readouts` is unchanged. `values` is an addition beside it.
