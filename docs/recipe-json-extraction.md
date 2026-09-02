# JSON extraction: the path language, and the tool stack behind it

**Status:** specification. Companion to [`recipe-contract-v2.md`](recipe-contract-v2.md) §2 and §7.

A recipe reads from four places: the DOM (CSS, XPath), the accessibility tree (`ax_role`), and
**JSON** — JSON-LD, hydration state (`__NEXT_DATA__`, Nuxt, Remix), and meta tags. This document
is about the third, because on modern commerce pages it is where most of the data actually is,
and because choosing its query language badly is a correctness problem, not a convenience one.

Everything below is measured against three real pages, loaded with real Chrome on 2026-09-02:

| Page | JSON-LD | Hydration | Verdict |
|---|---|---|---|
| **Zara** `…-p08004856.html` | 1 script, `@type: ProductGroup` | none | everything in one standard blob |
| **Walmart** `…/7843261295` | `WebPage` + `BreadcrumbList` only — **no `Product`** | **352 KB** `__NEXT_DATA__` | everything in a proprietary blob |
| **Amazon** `…/dp/B08J4FJ63D` | **0 scripts** | **none** | **no structured data at all** |

Those three outcomes are the entire argument for why candidate ordering is per-field and
per-page rather than a global policy. "Prefer JSON-LD" is excellent advice that would collect
*nothing* on Amazon, and would miss every one of Walmart's six spec sections. A recipe format
that assumes one of these shapes is a recipe format for one retailer.

---

## 1. Why this matters more than it looks

The Walmart page renders six accordion sections: *Product details*, *Specifications*,
*Indications*, *Directions*, *Ingredients*, *About the brand*. They look like six click-to-expand
reveal problems.

They are not. **All six are already in `__NEXT_DATA__`, fully populated, before any click:**

| Section | Path under `props.pageProps.initialData.data` |
|---|---|
| Product details | `idml.longDescription` (HTML), `idml.shortDescription` |
| Specifications | `idml.specifications[]` → `{name, value}` ×10, plus `idml.specificationsV2[]` |
| Indications | `idml.indications[]` → `{name, value}` ×3 |
| Directions | `idml.directions[]` → `{name, value}` ×1 |
| Ingredients | `idml.ingredients.{ingredients,inactiveIngredients,activeIngredients}` |
| About the brand | `product.brand`, `product.brandUrl` |

A recipe that clicks six accordions to scrape rendered text is doing six times the work, taking
six times the flake, and producing worse data than one structured read. This is the single most
valuable thing the JSON path is for, and it is why candidate ordering in the contract puts
`json_ld`/`hydration`/`meta` ahead of `css` by default.

The same lesson on Zara: the entire size/price/stock table — five variants with individual SKUs,
prices, currencies and `schema.org` availability — is in `hasVariant[]`. Clicking five size
swatches to reconstruct it would be strictly worse.

**Rule: if the data is already in the page's JSON, do not click for it.**

---

## 2. The requirement, stated honestly

`recipe/jsonpath.py` today is 31 lines of dict-key/list-index traversal. It resolves
`[0].offers.price`. It cannot do any of the following, all of which the Walmart page needs:

1. **Filter by a sibling key** — "the specification whose `name` is `Scent`". The specs are an
   unordered array of `{name, value}`; positional indexing is not stable across products.
2. **Project an array to a map** — turn `[{name, value}, …]` into `{name: value}`, which is the
   shape a caller actually wants for a spec sheet.
3. **Flatten nested groups** — `specificationsV2[].specificationGroup[]` is two levels of array.
4. **Take the first non-null of several candidates** inside one expression.

So the path language has to grow. The question is how far, and in which direction.

---

## 3. The candidates, measured

All four were run against the real 352 KB Walmart blob.

### JMESPath — **the recommendation**

```
props.pageProps.initialData.data.product.name
  -> "Dove Body Wash Strawberry Cookie 20 fl oz"                        (0.0 ms)

props.pageProps.initialData.data.idml.specifications[?name=='Scent'].value | [0]
  -> "Strawberry Cookie"                                                (0.1 ms)

props.pageProps.initialData.data.idml.specifications[].{k:name,v:value}
  -> [{"k":"Primary ingredient","v":"Strawberry Crumb Cake"}, …]        (0.1 ms)

props.pageProps.initialData.data.idml.specificationsV2[].specificationGroup[].{k:displayName,v:attributeValue[0]}
  -> [{"k":"Primary ingredient","v":"Strawberry Crumb Cake"}, …]        (0.0 ms)

props.pageProps.initialData.data.idml.indications[].name
  -> ["Stop Use Indications","Health Concern","Skin Care Concern"]      (0.0 ms)
```

Every requirement in §2, sub-millisecond, on the largest blob we have.

**Why it wins:**

- **It is a real specification** with a published compliance suite, not a family of mutually
  incompatible dialects. That matters enormously when an LLM is writing the expressions: the
  grammar is small enough to put in a prompt, and "valid JMESPath" is a checkable property.
- **Pure Python, no C extension.** It compiles expressions to an AST and interprets them; there is
  no `eval`, no code execution, no sandbox needed. Contrast with the Lua transform, which needs a
  whole threat model.
- **Already in the dependency graph.** `jmespath` appears in `uv.lock` today (via
  `boto3`/`botocore`). Promoting it to a direct dependency of `agentpilot` adds a name to a
  `pyproject.toml`, not a new package to vet.
- **Its biggest apparent weakness is actually a safety property.** See below.

### JSONPath (RFC 9535, e.g. `jsonpath-ng`, `python-jsonpath`) — rejected

JSONPath was standardised as RFC 9535 in 2024 and is a reasonable language. Its distinguishing
feature over JMESPath is the **descendant segment** `..`, which searches at any depth. That
feature is precisely why it is rejected here. Measured on the Walmart page:

```
$..product.name  ->   1 match   ["Dove Body Wash Strawberry Cookie 20 fl oz"]
$..name          -> 203 matches, first distinct strings:
                       " 3Grid AddToCart Sticky"
                       "3 Grid Below Sticky BuyBox Ad Module"
                       "Dove Body Wash Strawberry Cookie 20 fl oz"
                       "St. Ives Soothing Body Wash for Women, Oatmeal & Shea Butter, 22 fl oz"
                       "fulfillment"
```

The fourth distinct result is **a competitor's advertisement**, injected into the page's own
JSON under `contentLayout.modules[1].configs.ad.adContentV2.data.products[0]`. An unanchored
`$..name` or `$..price` on this page does not fail — it succeeds, with well-typed, plausible,
*wrong* data, forever. That is failure class 4 in
[`recipe-operations.md`](recipe-operations.md) §D1, the one that silently poisons a dataset, and
here it is not hypothetical.

`$..product.name` returning exactly one match is luck: it holds only while there is exactly one
`product` key anywhere in a 352 KB document that Walmart controls and we do not.

A model authoring recipes will reach for `..` because it is the shortest thing that works on the
page in front of it. Refusing to offer it is the cheapest available defence, and JMESPath refuses
by design — its authors omitted recursive descent deliberately.

If a future case genuinely needs descent, the answer is a `find` transform with an explicit
anchor and an explicit expectation of match count — not a language where the footgun is the
shortest path.

### jq (`jq.py`, C bindings) — rejected

The most expressive option, and genuinely excellent for ad-hoc work. Rejected for three reasons:
it is a Turing-complete language, and this system would then embed **two** (Lua is already the
sanctioned escape hatch — a second is confusing, not powerful); it requires a C build dependency,
which the worker image does not need and the Chrome-free gateway image should never carry; and
its expressions are far harder to review than a path.

### `glom` — rejected

Pleasant, Python-native, composable. But it is a library-specific spec rather than a standard, it
is much less likely to be in a model's training distribution than JMESPath, and its specs are
Python objects rather than strings — awkward to store in a JSONB recipe document.

---

## 4. The decision

**Two path dialects, selected per locator by `path_lang`.**

```json
{ "kind": "hydration", "path": "props.pageProps.initialData.data.product.name" }

{ "kind": "hydration", "path_lang": "jmespath",
  "path": "props.pageProps.initialData.data.idml.specifications[?name=='Scent'].value | [0]" }
```

| | `simple` (default) | `jmespath` |
|---|---|---|
| Syntax | `a.b[0].c` | full JMESPath |
| Implementation | today's `recipe/jsonpath.py`, unchanged | `jmespath` library |
| Wildcards / filters / projections | no | yes |
| Recursive descent | no | no |
| When | the path is known and shallow | the data is a haystack |

`simple` stays the default and stays weak on purpose. Most paths are literally
`[0].offers.price`, and a path a reviewer can read at a glance is worth more than one that can
express anything. `jmespath` is opt-in per locator, so a recipe's complexity is visible in its
own document rather than hidden in a dialect flag at the top.

**Neither dialect offers recursive descent.** That is the load-bearing decision of this document.

### Dependency change

```toml
# packages/agentpilot/pyproject.toml
dependencies = [ ..., "jmespath>=1.0" ]
```

Already in `uv.lock` transitively; this promotes it to direct. Pure Python, no extension module,
so the Chrome-free gateway image is unaffected. It belongs to `agentpilot`, not `crawlpilot` —
`crawlpilot` is the browser library and has no business knowing how a recipe addresses JSON.

---

## 5. Where the JSON comes from — no new fetching

The recipe layer does not parse pages itself. It issues one
`ExtractAction(format="structured_data")` per group, which
`crawlpilot/extraction/structured_data.py` already answers with three containers:

| Container | Contents |
|---|---|
| `json_ld` | every `<script type="application/ld+json">`, parsed, as a list |
| `hydration` | `__NEXT_DATA__`, Nuxt state, and — when no hydration script is present — a live JS-eval probe for client-only SPA globals |
| `metadata` | OpenGraph, Twitter, Dublin Core, and plain `<meta name>` tags |

`Locator.kind` selects the container; `path` + `path_lang` address into it. On Zara that yields a
`ProductGroup` under `json_ld[0]`; on Walmart it yields the 352 KB `__NEXT_DATA__` under
`hydration`. Both are one read.

`fetch_structured_data` is called **once per field group** and shared across every field in it, so
a group reading twelve fields out of `__NEXT_DATA__` costs one extraction, not twelve.

---

## 6. Two adjacent problems this surfaced

### 6.1 JSON fields that contain HTML

`idml.longDescription` is a *string* containing markup:

```html
<ul>  <li>Crumbl for Strawberry Crumb Cake cookies…now in a Body Wash</li>  <li>Notes of rich
strawberry cookie topped with vanilla glaze &amp; buttery crumbs for the shower</li> …
```

Reading it as a string yields tags and `&amp;`. This is common enough — most PIM-backed commerce
JSON carries HTML descriptions — to deserve a first-class transform rather than a Lua snippet:

```json
{"op": "strip_html"}
```

Unescapes entities and flattens block elements to newlines. Added to
[`recipe-contract-v2.md`](recipe-contract-v2.md) §7. It uses `lxml`, already a `crawlpilot`
dependency for the extraction pipeline.

### 6.2 Enum-ish strings that want to be booleans

Both pages encode availability as a vocabulary string, in different vocabularies:

- Zara: `"https://schema.org/InStock"` / `"https://schema.org/OutOfStock"`
- Walmart: `product.availabilityStatus` = `"OUT_OF_STOCK"`

`map_lookup` handles both without a regex or a script, and — importantly — its `default` makes the
unmapped case explicit rather than silently falsy:

```json
[{"op": "regex_extract", "pattern": "([^/]+)$", "group": 1},
 {"op": "map_lookup",
  "table": {"InStock": true, "OutOfStock": false, "IN_STOCK": true, "OUT_OF_STOCK": false},
  "default": null}]
```

A `null` here is honest — "this page said something we have not seen before" — and an assertion of
`{"kind": "not_empty"}` turns it into a `suspect` field rather than a confident lie.

---

### 6.3 When there is no JSON at all

Amazon is the control case: 0 JSON-LD scripts, no `__NEXT_DATA__`, no hydration globals, and 5
meta tags of which none are commercial. Everything must come from the DOM. Two consequences for
the contract, both borne out by
[`examples/recipes/amazon-product.v2.json`](examples/recipes/amazon-product.v2.json):

- **XPath stops being optional.** The spec rows are `<tr><th>Key</th><td>Value</td></tr>` inside
  seven identically-classed, id-less tables. Selecting *the `td` whose sibling `th` says
  "Item Weight"* is the one thing CSS genuinely cannot express, and it is the natural shape of
  every spec table on the web. This is the concrete justification for the driver-side
  `document.evaluate` work.
- **`to_object` carries the same weight it does for JSON.** Whether the pairs arrive as
  `[{name, value}]` from `idml.specifications` or as `th`/`td` text from a table, the caller
  wants `{name: value}`. One transform serves both, which is what keeps the two extraction paths
  from diverging into two dialects.

The mirror-image lesson to §1: on Walmart, six sections *look* like clicks and are really JSON.
On Amazon, four sections (*Features & Specs*, *Style*, *Measurements*, *Additional details*)
*look* like clicks and are really **already-present DOM** — collapsed, not absent, and readable
through `textContent` without expanding anything. See
[`recipe-contract-v2.md`](recipe-contract-v2.md) §2, "text vs visible_text".

Three pages, three different right answers, and in none of them is the obvious one correct.

## 7. Summary of changes this document requires

| Change | Where |
|---|---|
| `Locator.path_lang: "simple" \| "jmespath"` | contract §2, schema `Locator` |
| `json_path` transform gains `path_lang` | contract §7, schema `Transform` |
| New `strip_html` transform | contract §7, schema `Transform` |
| `jmespath>=1.0` as a direct dependency | `packages/agentpilot/pyproject.toml` |
| No recursive descent, in either dialect | this document, §3 |
