# Interacting with a page

## Two ways to say *which* element

```python
page.click("#add-to-cart")     # positional  -> CSS selector
page.click(ref="e42")          # keyword     -> snapshot ref
page.click("#x", ref="e42")    # -> ValueError
```

**The parameter decides, never the string.** There is no sniffing, and there
cannot be: a ref is `e<index>`, and `e42` is *itself a valid CSS type selector*
— it matches an `<e42>` custom element. Any rule based on the shape of the
string would be a guess, and a wrong guess acts on the wrong element without
telling you. So a positional target is always CSS, `ref=` is always a ref, and
passing both is an error rather than a silent preference.

Node ids never appear in the API. `backendNodeId` is an integer the driver owns;
your vocabulary is exactly two things.

## Which should you use?

| | CSS selector | Snapshot ref |
|---|---|---|
| Needs a `snapshot()` first | no | yes |
| Cross-origin iframes | **cannot reach** | yes |
| Shadow DOM | **cannot reach** | yes |
| Ambiguous match | takes the first silently | impossible — a ref is one node |
| Survives a page change | re-queried each time | invalidated, raises `StaleRefError` |

Selectors are resolved by `DOM.querySelector`, which is document-scoped — hence
the two "cannot reach" rows. Refs are a dictionary lookup against the tree the
snapshot captured, and the driver then acts on that node's
`(session, backendNodeId)` directly.

**Use a selector** when you know the page and want one line. **Use a ref** when
the page is unfamiliar, uses iframes or shadow DOM, or when a model is choosing
the element — which is why the agent tools are refs-only.

## Snapshots and refs

```python
snapshot = await page.snapshot()
print(snapshot.llm_text)
```

```
[e14]<button "Add to cart" id=add-to-cart />
[e16]<button "Save for later" id=wishlist />
[e23]<checkbox "Gift wrap" id=gift type=checkbox />
[e25]<textbox "Promo code" id=promo type=text />
```

Each `[eNN]` is a ref you can pass as `ref=`. `snapshot.refs` maps each one to
its accessible role, name and box, so picking one is a dict scan:

```python
ref = next(r for r, info in snapshot.refs.items() if info.name == "Add to cart")
await page.click(ref=ref)
```

A `Snapshot` is the same on a local browser and a remote one. If you need the
fused DOM itself — every node and attribute, to walk or serialize yourself —
that is `await page.tree()`, and it exists only in-process:

```python
from crawlpilot.spi.dom_tree import iter_elements

tree = await page.tree()
ids = [n.attributes.get("id") for n in iter_elements(tree)]
```

**Refs are epoch-scoped.** Every snapshot and every navigation clears the index,
so a ref from a superseded capture raises `StaleRefError` rather than resolving
against a DOM that has moved on. Re-snapshot after anything that changes the
page.

## Reading the page

Every getter returns a real Python value:

```python
await page.get_title()                      # -> str
await page.get_count(".item")               # -> int
await page.is_visible(selector="#banner")   # -> bool
await page.is_checked(selector="#gift")     # -> bool | None   (None = indeterminate)
await page.get_attribute("href", selector="a")   # -> str | None  (None = absent)
await page.get_styles(selector="#x")        # -> dict[str, str]
await page.search_page("out of stock")      # -> list[str]
await page.find_elements("a.product")       # -> list[dict]
```

Before 0.2 these returned prose written for an LLM prompt. See
[migration-0.2.md](migration-0.2.md) — in particular `is_visible()`, which
returned a truthy string in both directions.

The prose still exists, on `ActionResult.readouts`, because that is what an
agent puts in its context. `ActionResult.values` is the same answers as values,
index-correlated, and it is what the getters read.

## Batching

Every convenience method composes one action and dispatches it. When you want
several in one round trip, use `execute()` directly — it is the primitive the
rest is sugar over:

```python
from crawlpilot.spi import actions as a

result = await page.execute([
    a.NavigateAction(url="https://example.com/search"),
    a.FillAction(selector="#q", text="widgets"),
    a.ClickAction(selector="button[type=submit]"),
    a.ExtractAction(format="markdown"),
])
print(result.extracts[0])
```

`ActionResult` carries per-type, index-correlated lists: `extracts`,
`screenshots`, `values`, `readouts`, `snapshots`, `verifications`. One batch,
one round trip — which is the difference that matters at volume.

## Waiting

Each of these raises `WaitTimeout` rather than returning a flag, so the call
after it can rely on the state it asked for:

```python
await page.wait_for_selector("#results", timeout_ms=10_000)
await page.wait_for_text("In stock")
await page.wait_for_url("**/checkout")
await page.wait_for_load("networkidle")
```

## Errors worth catching

| Exception | Means |
|---|---|
| `SelectorNotFound` | the CSS selector matched nothing |
| `StaleRefError` | the ref's snapshot was superseded, or the node is gone |
| `WaitTimeout` | a `wait_for_*` condition never came true |
| `ChallengeDetected` | a bot wall — see [anti-detection.md](anti-detection.md) |
| `NavigationTimeout` | the page never reached the requested load state |

All of them subclass `DriverError`, so `except DriverError` is the one broad
catch that covers the library's own failures.

Note that it does **not** cover everything a scrape can raise: a dead host
surfaces as the underlying automation library's own error type. `batch_scrape`
catches broadly for exactly this reason and records the failure on
`Document.error`.
