# Recipe Contract v2

**Status:** specification, not yet implemented. Normative for the v2 rewrite.
**Companions:** [`recipe-operations.md`](recipe-operations.md) (why these fields exist),
[`recipe-studio.md`](recipe-studio.md) (how they are authored),
[`domain-agents.md`](domain-agents.md) (how they are published),
[`recipe-contract-v2-cleanup.md`](recipe-contract-v2-cleanup.md) (what v1 code goes away),
[`schemas/recipe-v2.schema.json`](schemas/recipe-v2.schema.json) (machine-readable form).

---

## What a recipe is

A recipe is a **versioned, persisted, URL-independent description of how to collect a
caller-declared set of fields from one kind of page**. It is authored once — by an LLM
exploration run, a human in the studio, or both — and then replayed deterministically, with no
model call, for as long as it keeps working.

The whole design follows from one economic fact: a model call per page does not scale, and a
selector written once does. So the model is spent at *authoring* time and at *repair* time, never
at collection time. Everything in this contract exists either to make a recipe expressive enough
to author, or to make its failure detectable enough to repair.

Two invariants hold throughout, and both are inherited from v1 because both were right:

1. **Nothing addressable is stored.** A `ref` minted during a live run is epoch-scoped and
   meaningless against a fresh page load. Every locator in a recipe is a *descriptor* re-resolved
   against the live page at replay time.
2. **Structured data beats the DOM.** A value available in JSON-LD, a hydration payload, or a meta
   tag survives a redesign that destroys every CSS selector on the page. Candidate ordering
   encodes that preference explicitly (§6).

---

## 1. Run input — the URL is not part of the recipe

A recipe describes a *page type*, not a page. The URL arrives per run, alongside whatever context
the caller wants to carry with it.

```
RecipeRunInput
  url:      str                  # the page to collect from
  metadata: dict[str, Any]       # arbitrary caller context, travels with the URL
```

`metadata` is what lets one recipe serve a whole catalogue: a scheduled job emits
`{url, {sku, region, currency}}` rows and the same recipe handles all of them.

**Templating.** `{{meta.<key>}}` is substituted in exactly three places, and nowhere else:

- a `Step`'s `args` string values,
- a `template` transform's format string,
- a `Predicate`'s operand.

Substitution is textual and happens before dispatch. A missing key resolves to the empty string
and is recorded in `step_trace`; it is never an error, because a recipe that hard-fails on an
absent optional hint is worse than one that proceeds without it.

`metadata` is **never** interpolated into a `selector`, an `xpath`, or Lua source. Those are the
three places where a caller-supplied string would become executable, and the boundary is worth
more than the flexibility.

### TargetSpec — a guard, not a navigation target

```
TargetSpec
  match: list[UrlMatcher]        # {kind: "glob" | "regex" | "host", pattern: str}
```

`match` answers "does this recipe apply to this URL?". A run whose `url` matches nothing is
rejected **before a browser is opened** — the cheapest possible failure. An empty `match` means
"applies to any URL", which is legitimate for a generic recipe and suspicious for a site-specific
one; the studio warns, the contract permits.

This replaces v1's `Recipe.url_pattern`, which was used literally as the navigate target and
therefore pinned every recipe to the single page it was built on.

`TargetSpec` is also the routing key for domain agents: given a URL, an agent picks the first
bound recipe whose `match` accepts it.

---

## 2. Locator — one type for actions and reads

v1 had two divergent types (`Locator` for actions, `FieldLocator` for reads) with different source
unions and no shared base. v2 has one.

```
Locator
  kind: "css" | "xpath" | "ax_role" | "text" | "json_ld" | "hydration" | "meta"

  # css / xpath
  selector:  str
  index:     int | None          # nth match, 0-based; null means first
  all:       bool                # return every match (feeds list-typed fields)
  attribute: str                 # "text" | "visible_text" | "html" | "value" | "href" | any DOM attribute

  # ax_role
  role:          str
  name_contains: str | None
  name_in:       list[str] | None
  name_regex:    str | None

  # json_ld / hydration / meta
  path:      str                 # dotted/bracket path, or a JMESPath expression
  path_lang: "simple" | "jmespath"    # default "simple"

  # text
  text: str                      # visible-text match; resolves to its containing element

  # scoping — applies to css / xpath / ax_role / text
  within: Locator | None         # resolve inside this container first
  frame:  str | None             # iframe selector or name
```

### Read semantics per kind

| kind | resolves against | returns |
|---|---|---|
| `css` | live DOM, `document.querySelectorAll` | the `attribute` of the match at `index` (or all matches when `all`) |
| `xpath` | live DOM, `document.evaluate` | same |
| `ax_role` | the fused AX+DOM tree | the matched node's `attribute` — **`text` yields the accessible name** |
| `text` | the fused tree | the containing element's `attribute` |
| `json_ld` | `ExtractAction(format="structured_data")` → `json_ld` | the value at `path` |
| `hydration` | same → `hydration` (`__NEXT_DATA__`, Nuxt state, and a live JS-eval probe for client-only SPA state) | the value at `path` |
| `meta` | same → `metadata` (OpenGraph, Twitter, Dublin Core) | the value at `path` |

### `text` vs `visible_text` — a distinction worth a click

`attribute: "text"` reads **`textContent`, minus `<script>` / `<style>` / `<noscript>` /
`<template>` subtrees**. `attribute: "visible_text"` reads **`innerText`**. They differ in exactly
one way that matters: `text` returns the content of elements that are in the DOM but not rendered;
`innerText` does not.

The script exclusion is not fastidiousness. Raw `textContent` includes the *source* of any inline
script inside the element, and on real pages that is routine rather than exotic: Amazon's
`#availability` contains a `P.when(...)` block, and its *Customer Reviews* specification row
carries an inline click handler. Reading either raw returns a well-formed, plausible, entirely
useless value — failure class 4 in [`recipe-operations.md`](recipe-operations.md) §D1, arriving
by accident. `innerText` excludes them for free but also excludes collapsed content, which is the
one thing `text` exists to reach, so the reader walks the subtree itself.

That difference decides whether a reveal step is necessary at all. Measured on an Amazon product
page, its four collapsed accordion sections — *Features & Specs*, *Style*, *Measurements*,
*Additional details* — are `aria-expanded="false"` and contribute nothing to `document.body.innerText`
(expanding them grew it by +303, +73, +97 and +22 characters respectively). Yet all of their rows
are already in the DOM and are readable through `textContent` **without any click at all**.

So:

- Use `text` (the default) to read data that is present but collapsed. It is faster, it cannot
  race, and it does not mutate the page.
- Use `visible_text` when you specifically mean "what a user can actually see" — and pair it with
  a reveal step, because that is now a real precondition.
- A `wait_for_selector` with `state: "visible"` on a collapsed section will time out even though
  the data is right there. That is not a bug; it is the two meanings of "present" diverging.

The general rule this produces mirrors the one for JSON in §6: **if the data is already in the
DOM, do not click for it.** Clicking is for content that does not exist yet, not for content that
merely is not painted.

Three deliberate changes from v1:

- **`ax_role` can now read attributes.** v1 returned `matches[0].ax_name` unconditionally, so an
  `ax_role` locator could never read an `href`. The fused node carries its attributes; v2 reads
  them, with `attribute: "text"` preserving the old accessible-name behaviour as the default.
- **`within` scopes the match.** v1's `name_in` matched the whole tree, which its own docstring
  flagged as an accepted false-positive risk: a same-named element elsewhere on the page could be
  picked up in place of the intended option. `within` makes the container explicit.
- **`index` and `all` exist.** v1 could only ever read the first match, which is why list-typed
  fields had to go through the repeat machinery even when a plain `querySelectorAll` would do.

### Path languages, and why the default is deliberately weak

`path_lang: "simple"` is v1's dotted/bracket traversal (`[0].offers.price`) — no wildcards, no
filters, no descent. It stays the default because most paths are simple, and a readable path is a
reviewable path.

`path_lang: "jmespath"` unlocks filters, projections and multiselects for the pages where the
data is a haystack rather than a document:

```
props.pageProps.initialData.data.idml.specifications[?name=='Scent'].value | [0]
props.pageProps.initialData.data.idml.specificationsV2[].specificationGroup[].{k:displayName,v:attributeValue[0]}
```

The reasoning behind that choice, including the measured comparison against JSONPath and jq and
the reason recursive descent is **not** offered, is in
[`recipe-json-extraction.md`](recipe-json-extraction.md). The short version: on a real Walmart
product page, `$..name` matches 203 nodes, and the fourth distinct string it returns is a
competitor's advertisement. An extraction language whose easiest expression silently scrapes the
wrong brand is the wrong default.

### XPath is a driver prerequisite

Selectors resolve through `document.querySelector` in
`packages/crawlpilot/src/crawlpilot/driver/queries.py` — **not** the Playwright locator engine — so
`xpath=` is not free. Supporting `kind: "xpath"` requires a `document.evaluate` branch there, in
`crawlpilot`, which is `strict = true` under mypy and whose tool surface is golden-tested. This is
the only capability in v2 that the driver cannot already do; everything else is exposure.

---

## 3. Step — actions, waits, timeouts, error policy

A `Step` is one unit of "make the data reachable". Steps appear in `Recipe.global_setup` (run
before every field group) and in `FieldGroup.steps` (run for that group only).

```
Step
  op:           StepOp
  target:       Locator | None
  args:         dict                  # op-specific, see the table below
  timeout_ms:   int | None            # null inherits ExecutionDefaults
  retry:        {attempts: int, backoff_ms: int} | None
  on_error:     "fail" | "continue" | "skip_group"     # default "fail"
  optional:     bool                  # sugar for on_error = "continue"
  when:         list[Predicate]       # all must hold, else the step is skipped
  repeat_until: Predicate | None
  max_repeats:  int                   # required when repeat_until is set
  label:        str | None            # human-facing, shown in step_trace
```

### StepOp → catalog verb

Every op is an existing entry in `packages/crawlpilot/src/crawlpilot/tools/catalog.py` (64 verbs).
Nothing here is invented; the recipe layer simply could not reach most of it.

| StepOp | args | notes |
|---|---|---|
| `navigate` | `url`, `wait_until` | rarely needed — the runner navigates to `RecipeRunInput.url` |
| `click` | `all: bool` | uses `ClickAction(selector=…)`, a real trusted click |
| `double_click` | — | |
| `hover` | — | |
| `fill` | `text`, `clear: bool` | `text` is templatable |
| `clear` | — | |
| `press` | `key` | |
| `send_keys` | `keys` | modifier combinations |
| `select_option` | `values: list[str]` | |
| `check` / `uncheck` | — | |
| `scroll` | `direction`, `pages: float` | **direction is preserved** (v1 always sent `down`) |
| `scroll_into_view` | — | |
| `find_text` | `text` | |
| `drag` | `to` (a `Locator`) | |
| `tap` / `swipe` | `direction`, `distance` | |
| `wait` | `ms` | a fixed sleep — the op of last resort |
| `wait_for_selector` | `state: visible\|hidden\|attached\|detached` | |
| `wait_for_text` | `text` | |
| `wait_for_url` | `url` | glob or substring |
| `wait_for_load` | `state: load\|domcontentloaded\|networkidle` | |
| `dialog_accept` | `prompt_text` | |
| `dialog_dismiss` | — | |
| `new_tab` / `switch_tab` / `close_tab` | `url` / `page_id` | |
| `download` | — | |

`wait_for_function` and `execute_js` are **deliberately excluded**. Both are `safety="sensitive"`
in the catalog and both amount to running arbitrary JS on a schedule; a recipe is authored by a
model reading an untrusted page, so neither belongs in the vocabulary. Lua (§7) is the sanctioned
escape hatch, and it cannot touch the page.

### Waiting

The four `wait_for_*` ops carry the SPI's own guarantee, quoted here because `on_error` interacts
with it:

> Each takes a timeout and fails loudly when it expires. A wait that silently gave up would be
> worse than no wait at all: the action after it would run against the state the caller was
> waiting *not* to see, and report success.

So `wait_for_selector` with `on_error: "continue"` is a legitimate but sharp construct — it means
"proceed even if this never appeared", and the fields that depend on it will fail individually
rather than as a group. `wait` (a fixed sleep) is always the wrong answer when a `wait_for_*` will
do, and the studio flags it.

### Error policy

| `on_error` | effect |
|---|---|
| `fail` | the run's outcome becomes `failed`; remaining groups are not attempted |
| `continue` | the step is recorded as `failed` in `step_trace`, execution proceeds |
| `skip_group` | this group's fields are marked `failed` with the step's reason; other groups run |

`skip_group` is the right default for a reveal step guarding an optional section — a product page
without a *Measurements* button should lose the measurements, not the price.

`retry` applies **only** to the step itself and only for transient failures (timeout, stale
element). It never retries a step that succeeded, and never re-runs an earlier step; a step batch
is not idempotent, which is the same reason the agent loop refuses to retry action dispatch.

### Repetition

`repeat_until` + `max_repeats` expresses the common lazy-load pattern:

```json
{ "op": "scroll", "args": {"direction": "down", "pages": 1},
  "repeat_until": {"kind": "count_at_least", "selector": ".product-card", "n": 60},
  "max_repeats": 20 }
```

Hitting `max_repeats` without satisfying the predicate is **not** silent: it sets
`truncated` for the affected group in the result (§9). This is the completeness rule stated in
[`recipe-operations.md`](recipe-operations.md) §D2 — a recipe that cannot report finding too
little is worse than one that fails.

---

## 4. Predicate

One type, three uses: step `when`, step `repeat_until`, and variant `detect`.

```
Predicate
  kind: "selector_present" | "selector_absent" | "visible" | "text_present"
      | "url_matches"      | "json_path_present" | "count_at_least" | "meta_equals"
  selector: str | None       # selector_present / selector_absent / visible / count_at_least
  text:     str | None       # text_present
  url:      str | None       # url_matches — glob
  path:     str | None       # json_path_present
  source:   str | None       # json_path_present: json_ld | hydration | meta
  key:      str | None       # meta_equals — a RecipeRunInput.metadata key
  value:    str | None       # meta_equals — templatable
  n:        int | None       # count_at_least
```

Predicates are evaluated against the live page and are side-effect free. A predicate that cannot
be evaluated (a malformed selector, a missing structured-data container) is **false**, never an
error — a guard that explodes is worse than a guard that declines.

---

## 5. Page variants

The same site serves different layouts: an A/B test, a legacy template, a regional variation, an
out-of-stock rendering. Variants let one recipe carry selectors for all of them.

```
PageVariant
  variant_id: str
  priority:   int                # lower is checked first
  detect:     list[Predicate]    # all must hold
  label:      str | None
```

Variants are evaluated **once**, after `global_setup`, in `priority` order. The first fully
satisfied variant is the active one for the whole run, and is recorded in every field's
`provenance`.

If no variant matches, the run proceeds with `variant_id = null` and only unscoped candidates
apply. This is **degraded, not failed** — a recipe should still collect whatever its
variant-agnostic candidates can reach. It is also a heal trigger: "no variant matched" usually
means the site shipped a layout nobody has seen yet.

---

## 6. Field bindings — ordered candidates

This is the core of the "multiple selectors per attribute, in priority order" requirement.

```
FieldGroup
  group_id:   str
  field_names: list[str]
  steps:      list[Step]
  bindings:   dict[str, list[Candidate]]     # field name -> ordered candidates
  repeat:     RepeatSpec | None
  expect:     {min_rows: int | None, max_rows: int | None} | None
```

```
Candidate
  priority:    int                     # lower is tried first
  locator:     Locator
  when:        list[Predicate]         # candidate-level guard
  variant_id:  str | None              # only applies under this variant
  transform:   list[Transform] | None  # overrides the field's transform for this candidate
  verified_on: int                     # how many sample URLs this resolved on at build time
  confidence:  float | None            # advisory, written by build/heal
  note:        str | None
```

### Resolution order (normative)

1. Drop candidates whose `variant_id` is set and does not equal the active variant.
2. Drop candidates whose `when` predicates are not all satisfied.
3. Sort the survivors by `priority` ascending; **ties break by list order**, so an author who
   never sets `priority` gets exactly v1's behaviour.
4. Evaluate in order. **The first non-empty result wins.** Empty means `null`, `""`, or a
   whitespace-only string.
5. If a candidate has its own `transform`, apply that; otherwise apply the field's.
6. If every candidate is empty, the field's value is `null` and its status is `empty` — or
   `failed` if the field is `required`.

Step 4 is v1's coalesce semantics (Pulsar's `array_first_not_blank` / comma-union idiom), kept
intact. What v2 adds is that the ordering is now *stated* rather than implied by list position,
and can be overridden without reordering the array — which matters because build, heal, and a
human in the studio all write to the same list.

### Why per-candidate transforms

The same field read from two sources needs different cleanup:

```json
"price": [
  {"priority": 10, "locator": {"kind": "json_ld", "path": "[0].offers.price"},
   "transform": [{"op": "cast", "to": "float"}]},
  {"priority": 20, "locator": {"kind": "css", "selector": ".price-current", "attribute": "text"},
   "transform": [{"op": "regex_extract", "pattern": "([\\d.,]+)"},
                 {"op": "regex_replace", "pattern": ",", "repl": ""},
                 {"op": "cast", "to": "float"}]}
]
```

JSON-LD gives `"1299.00"`; the DOM gives `"₹ 1,299.00"`. One field-level transform cannot serve
both without being written for the worse case and silently mangling the better one.

### RepeatSpec — producing the rows of a `table` field

A `table` field's rows come from one of two places, and conflating them was a v1 limitation that
only became obvious when writing the Zara example: v1 could *only* iterate DOM options by
clicking, so a table that already existed as a JSON array had to be faked as one.

```
RepeatSpec
  kind: "dom" | "json"

  # kind = "dom" — click through an option set, read the page after each
  option_locator: Locator       # must match MULTIPLE elements at replay time
  action:         StepOp        # default "click"
  settle:         Step | None   # e.g. wait_for_selector after each option

  # kind = "json" — iterate an array already present in structured data
  rows_locator:   Locator       # kind json_ld/hydration/meta, path to the array

  # both
  max_iterations: int
  row_field:      str           # the table-typed field these rows populate
```

**`kind: "dom"`** is v1's behaviour, with two fixes:

- Re-resolve `option_locator` **before every iteration** — a prior click may have re-rendered the
  option set, invalidating earlier matches.
- v1 appended only *complete* rows and silently dropped partial ones. v2 appends every row and
  marks incomplete ones in `field_status`; dropping data to make a run look clean is the exact
  failure mode §9's `truncated` exists to prevent.
- `settle` closes the race v1 had no way to express: after clicking size *M*, the price node may
  not have updated yet, so the row captures size *M* with size *S*'s price.

**`kind: "json"`** is new. `rows_locator` resolves to an array; each element becomes a row, and
that element is the **root** for the column candidates, whose locators use `json_ld`-family paths
relative to it. No clicking, no page mutation, no ordering race, one structured-data read for the
whole table.

This matters more than it sounds. On the Zara product page, the entire size table — size label,
SKU, price, currency, and stock status for all five variants — is present in the page's
`ProductGroup` JSON-LD under `hasVariant[]`. Modelling it as a DOM repeat would mean five clicks,
five settles, and five re-reads to reproduce data that one structured-data read already contains,
while being far more fragile. `kind: "json"` is the difference between a recipe that costs one
page load and one that costs five plus a race condition.

The general rule, and the reason candidate ordering prefers structured data everywhere: **if the
data is already in the page's JSON, do not click for it.**

---

## 7. Transform pipeline

An ordered list, applied left to right. v1's `FieldNormalization` was a fixed-order struct; the
behaviour it encoded was right, the fixed order was the limitation.

```
Transform = one of:

  {op: "regex_extract",  pattern, group = 0, flags = ""}
  {op: "regex_replace",  pattern, repl, count = 0}
  {op: "trim"}
  {op: "collapse_ws"}
  {op: "strip_control"}
  {op: "strip_accents"}
  {op: "case",           mode: "lower" | "upper" | "title"}
  {op: "split",          sep, limit = -1}          # -> list
  {op: "join",           sep}                      # list -> str
  {op: "slice",          start, end}
  {op: "index",          i}                        # list -> element
  {op: "unique"}                                   # list -> list
  {op: "filter_empty"}                             # list -> list
  {op: "map_lookup",     table: {str: any}, default = null}
  {op: "template",       format}                   # "{{v}} cm", {{meta.*}} allowed
  {op: "url_resolve"}                              # against RecipeRunInput.url
  {op: "json_parse"}
  {op: "json_path",      path, path_lang = "simple"}
  {op: "strip_html"}                               # HTML fragment -> plain text
  {op: "html_select",    selector, attribute = "text", all = false}
                                                   # parse an HTML *string*, select from it
  {op: "to_object",      key, value}               # [{name,value}, …] -> {name: value}
  {op: "to_pairs",       key = "key", value = "value"}   # the inverse
  {op: "cast",           to: ValueType}
  {op: "default",        value}                    # substituted when the value is empty
  {op: "lua",            source}                   # §7.1
```

### Ordering is the author's problem, and that is the point

v1 applied `regex → replace → trim/case → coerce → default → required-gate` always. That is a good
default and a bad constraint: it cannot express "cast, then default" or "split, then trim each
part". v2 makes the order explicit. The migration of v1 semantics is mechanical — every v1
`FieldNormalization` maps to exactly this list, in this order — and the studio offers it as the
starting pipeline for a new field.

### Shaping ops: `to_object`, `html_select`

Three of the ops above exist because real pages forced them, and each replaces what would
otherwise be a Lua snippet — which matters, because a declarative op is reviewable, diffable, and
safe by construction where a script is none of those things.

**`to_object`** turns the near-universal `[{name, value}, …]` specification shape into the map a
caller actually asked for:

```json
"transform": [{"op": "to_object", "key": "name", "value": "value"}]
```
```
[{"name": "Scent", "value": "Strawberry Cookie"}, {"name": "Form", "value": "Liquid"}, …]
  ->  {"Scent": "Strawberry Cookie", "Form": "Liquid", …}
```

Pair it with `type: {kind: "object", properties: {}}` — an open map whose keys are the page's,
not the schema's. `to_pairs` is the inverse, for callers who want rows.

**`html_select`** parses an HTML *string* — one that arrived as a JSON value, not as the page —
and selects from it. Commerce JSON is full of these: Walmart's `idml.longDescription` is a JSON
string containing `<ul><li>…</li></ul>`, and the caller wants the bullets as a list:

```json
"transform": [{"op": "html_select", "selector": "li", "attribute": "text", "all": true},
              {"op": "filter_empty"}]
```

Without it, the only options are a regex over markup (fragile) or Lua (overkill). It uses `lxml`,
already a `crawlpilot` dependency.

### List semantics

Transforms operate on scalars by default. When the incoming value is a list (from `Locator.all`
or `split`), scalar ops **map over the elements**; `join`, `index`, `unique`, and `filter_empty`
operate on the list itself. This is the only implicit behaviour in the pipeline and it is stated
here because it would otherwise be a surprise.

### 7.1 The Lua escape hatch

Declarative ops cover the overwhelming majority of real normalization. They do not cover, for
example, turning `"Length: 120 cm / 47.2 in"` into `{"cm": 120.0, "in": 47.2}`. Rather than grow
the op list until it is a programming language with bad ergonomics, v2 admits one:

```json
{"op": "lua", "source": "local cm, inch = v:match('([%d%.]+)%s*cm.-([%d%.]+)%s*in')\nreturn {cm = tonumber(cm), inch = tonumber(inch)}"}
```

**Signature.** `v` is the incoming value; `ctx` is a read-only table
`{url, meta, field, variant, raw}`. The chunk returns the new value. Lua tables convert to JSON
objects (string keys) or arrays (1..n integer keys).

**Runtime.** `lupa` (LuaJIT). It is already a dependency of this repo — currently dev-only, for
testing the Redis Lua scripts — and moves to an optional extra `recipe-lua`.

**Sandbox.** A fresh Lua state per evaluation, with an allowlisted environment:

- **available:** `string`, `table`, `math`, `tonumber`, `tostring`, `type`, `select`, `ipairs`,
  `pairs`, `error`, `pcall`
- **removed:** `io`, `os`, `require`, `dofile`, `loadfile`, `load`, `loadstring`, `package`,
  `debug`, `collectgarbage`, `rawset`, `rawget`, `setmetatable`, `getmetatable`

**Limits.** An instruction-count hook, a wall-clock timeout, and a memory ceiling, all
configurable per tenant. Exceeding any of them, or raising, is a **field failure** — recorded with
a reason, never a crashed run and never a poisoned value.

**Determinism is required.** No network, no filesystem, no clock, no randomness. A transform that
returns a different value for the same input breaks replay reproducibility, makes a heal diff
meaningless, and makes a golden fixture a lie. This is not a sandbox side effect; it is the
contract.

**Gating.** Off by default. A tenant flag `recipe_lua_enabled` admits it; a recipe containing any
Lua carries `has_script: true` so it can be found, reviewed, and — per
[`domain-agents.md`](domain-agents.md) — held to a stricter publication gate.

**Threat model.** Recipe Lua is written by a model reading an untrusted web page, so it is treated
as untrusted input, not as trusted operator configuration. The sandbox is the enforcement; the
studio's human review is the second gate; the `published` lifecycle state is the third.

---

## 8. Type system

```
TypeSpec = {kind: "scalar", value_type: ValueType}
         | {kind: "list",   items: TypeSpec}
         | {kind: "object", properties: {str: TypeSpec}}
         | {kind: "table",  columns: {str: TypeSpec}}
```

```
ValueType = "string" | "text" | "number" | "float" | "integer" | "price"
          | "boolean" | "url" | "date" | "datetime" | "json"
```

`float` is an alias of `number` (both produce a Python `float`), present because callers ask for
it by name. `price` remains distinct from `number` not because it coerces differently but because
it tells the *author* which element to reach for — v1's `_type_hint` already uses value types to
steer locator proposal, and that is worth keeping.

`table` subsumes v1's `type: "array"` + `RepeatSpec` pairing. `object` and `list` are new and are
what make `map`/`list` output shapes expressible without abusing the repeat machinery.

```
FieldSpec
  name:        str
  description: str                  # read by the authoring model — write it for a reader
  type:        TypeSpec
  required:    bool
  emit_raw:    bool                 # also emit "<name>_raw" with the pre-transform value
  transform:   list[Transform]
  assertions:  list[Assertion]      # §10
```

---

## 9. Result model

```
RecipeRunResult
  outcome:        "ok" | "partial" | "failed" | "blocked"
  data:           {str: Any}
  field_status:   {str: "resolved" | "fallback" | "suspect" | "empty" | "failed"}
  provenance:     {str: {candidate: int, source: str, variant: str | None}}
  truncated:      {str: bool}
  assertions:     {str: [AssertionResult]}
  step_trace:     [StepOutcome]
  variant_id:     str | None
  error:          str | None
```

| `field_status` | meaning |
|---|---|
| `resolved` | the highest-priority applicable candidate produced a value |
| `fallback` | a lower-priority candidate produced it — the primary is drifting |
| `suspect` | a value was produced but failed an assertion or a cross-source check |
| `empty` | every candidate resolved empty; the field is optional |
| `failed` | required and empty, or its group was skipped, or a transform errored |

| `outcome` | meaning |
|---|---|
| `ok` | every field `resolved` or `fallback`, no assertion failures |
| `partial` | some fields `empty`/`suspect`, no required field `failed` |
| `failed` | a required field `failed`, or a `fail`-policy step failed |
| `blocked` | the page was classified as a bot wall / challenge — **see below** |

### `blocked` is a distinct outcome, and it is the most important field here

A CAPTCHA page has no product name, no price, and no measurements. Under v1's model that is
indistinguishable from "every selector broke", which is what makes the current heal path dangerous:
it re-explores against the challenge page and writes a new recipe version derived from it.

`blocked` breaks that chain. It is produced by classifying the page before evaluating fields, and
it **never triggers a heal**. Full reasoning, and the reason this is done recipe-side rather than
by re-enabling session-wide block detection, is in [`recipe-operations.md`](recipe-operations.md)
§D5.

### `provenance` and `truncated`

`provenance` records which candidate index, which source kind, and which variant produced each
value. It is what turns "the recipe still works" into "the recipe still works, but field `price`
has been resolving from candidate 2 instead of candidate 0 for six days" — drift visible before
breakage.

`truncated` marks a table field that hit `max_iterations` or `max_repeats`, or a group that failed
its `expect.min_rows`. A run that captured 8 of 40 sizes must never report `ok`.

```
StepOutcome
  index: int; op: str; label: str | None
  status: "ok" | "skipped" | "recovered" | "failed"
  duration_ms: int
  reason: str | None
```

---

## 10. Quality contract

Assertions are the cheap, model-free defence against the failure that silently poisons a dataset:
a selector that drifts onto the wrong element and keeps producing well-typed values forever.

```
Assertion = {kind: "range",       min, max}
          | {kind: "matches",     regex}
          | {kind: "in_set",      values: [any]}
          | {kind: "length",      min, max}
          | {kind: "not_empty"}
          | {kind: "cross_source_agrees", tolerance = 0}
          | {kind: "not_equals_previous"}
```

`cross_source_agrees` is the highest-value one and costs almost nothing: when a field has
candidates from two different `Locator.kind` families (say `json_ld` and `css`), evaluate both and
compare. Agreement is strong evidence the field is right; disagreement is strong evidence of
drift. Today both candidates already exist on most fields and only the first is ever read.

A failed assertion marks the field `suspect` — the value is still returned, because discarding
data on a heuristic is worse than flagging it, and `suspect` is what the monitoring in
[`recipe-operations.md`](recipe-operations.md) §D4 trends on.

Group-level:

```
FieldGroup.expect = {min_rows: int | None, max_rows: int | None}
```

### Recipe-level metadata

```
Recipe
  recipe_id, tenant, name, version
  status:      "draft" | "approved" | "published"
  target:      TargetSpec
  fields:      {str: FieldSpec}
  variants:    [PageVariant]
  global_setup: [Step]
  field_groups: [FieldGroup]
  defaults:    ExecutionDefaults
  sample_urls: [str]                      # pages this recipe was induced and verified on
  built_under: {tier: str, proxy_region: str | None}
  has_script:  bool
  health_status: "healthy" | "degraded" | "broken"
  heal_attempts: int
  last_verified_at, last_run_at, schedule_interval_seconds
```

```
ExecutionDefaults
  step_timeout_ms:      int = 10000
  navigate_timeout_ms:  int = 30000
  max_repeat_iterations: int = 20
  lua_timeout_ms:       int = 250
```

**`sample_urls` is required and must contain at least one entry, and should contain three.** A
recipe induced from a single page is an overfit guess; requiring the plural is the single
highest-leverage correctness change in v2, and the reasoning is in
[`recipe-operations.md`](recipe-operations.md) §D3–D4.

**`built_under` records the tier and proxy region.** The same URL serves different content by IP;
a recipe built through an Indian residential pool and replayed through a US datacenter pool may
legitimately find nothing.

**Lifecycle.** `draft` on creation. `approved` when a human accepts it in the studio. `published`
when it is bindable by a domain agent. Build and heal both write `draft`; nothing promotes
automatically.

---

## 11. Execution order (normative)

```
1. Reject if RecipeRunInput.url matches no TargetSpec.match         -> outcome = failed
2. Open a session; navigate to RecipeRunInput.url
3. Classify the page                                                -> blocked? stop, outcome = blocked
4. Run global_setup steps
5. Detect the active variant (first satisfied, by priority)
6. For each field group, on the SAME page load:
     a. run the group's steps        (reveals are guarded — see below)
     b. if repeat: iterate options, one row per option
        else:      evaluate each field's candidates
     c. apply transforms, assertions, required-gate
     d. run the group's teardown     (best-effort; never fails the group)
7. Assemble RecipeRunResult
```

**One page load per run.** v1, and v2 until this revision, re-navigated and re-ran `global_setup`
before every group so that state left by one group could not corrupt the next — the concrete
failure being a second drawer's button sitting under the first drawer, which is unclickable while
it is covered. It worked by throwing the page away, at *n* page loads for *n* groups: the slowest
part of a run, and on a protected site the shape of a bot.

The isolation it bought is now a contract the **recipe** carries, because the recipe is the thing
that knows what it opened:

- `steps` reach the state the group's fields are readable in;
- `teardown` returns the page to the state those steps started from — **required of any group whose
  steps change state**, and `validate_document` warns when one is missing;
- every reveal carries a `when` guard on the thing it reveals still being hidden, so a section an
  earlier group already opened is left alone rather than toggled shut.

`teardown` is best-effort: by the time it runs the values are collected, so a close control that has
moved leaves a dirty page rather than losing data already read. There is no fallback reload — a
recipe whose teardown does not restore the page fails and names the group, and `review` replays
sample URLs through the same executor before a recipe is saved, so the gap surfaces at build time.

Reloading during the **build** is unaffected: the agent and the assist path reload as often as they
need. What changed is that a reload is no longer part of a stored recipe.

---

## 12. What v2 fixes from v1, explicitly

| v1 behaviour | v2 |
|---|---|
| `url_pattern` used literally as the navigate target | URL is run input; `TargetSpec` is a guard (§1) |
| Two locator types, different source unions, no shared base | one `Locator` (§2) |
| `ax_role` could only ever return the accessible name | reads any attribute (§2) |
| `name_in` matched the whole tree — documented false-positive risk | `within` scoping (§2) |
| Only the first CSS match was readable | `index`, `all` (§2) |
| No XPath | `kind: "xpath"`, with a named driver prerequisite (§2) |
| 7 action verbs of 64 | ~28 ops, all existing catalog verbs (§3) |
| `scroll` silently discarded direction | `args.direction` preserved (§3) |
| CSS clicks compiled to `ExecuteJsAction` + `el.click()` — untrusted, no auto-wait, no scroll-into-view | `ClickAction(selector=…)` (§3) |
| Only `wait(ms)`; no wait-for-condition, no per-step timeout | four `wait_for_*` ops + `timeout_ms` (§3) |
| One `LocatorResolutionError` failed every field in the group | per-step `on_error` / `optional` / `retry` (§3) |
| Candidate order implicit in list position | explicit `priority`, `when`, `variant_id` (§6) |
| One transform pipeline per field, fixed order | ordered list, per-candidate override (§6, §7) |
| No scripting | sandboxed Lua (§7.1) |
| `scalar` / `array` only | `scalar`/`list`/`object`/`table`, `+float`, `+json` (§8) |
| Repeat silently dropped incomplete rows | rows kept, `truncated` reported (§6, §9) |
| Silent truncation at `max_iterations` | `truncated` (§9) |
| Blocked page indistinguishable from broken selectors | `outcome: "blocked"` (§9) |
| No provenance | `provenance` per field (§9) |
| No value-level validation | assertions, `suspect` status (§10) |
| Built and verified on exactly one URL | `sample_urls`, `verified_on` (§6, §10) |
| No lifecycle — a built recipe was immediately live | `draft → approved → published` (§10) |

---

## 13. Non-goals

- **Cross-group state dependency.** A group cannot depend on state left by another group. Groups
  now share a page load (§11), so this is a contract rather than a physical guarantee: each group
  reaches its own state from the page as `global_setup` left it, and returns it there. Ordering
  groups so that one relies on another's leftovers is outside the contract — a workflow that
  genuinely needs sequential state is an agent task, not a recipe.
- **Authentication flows.** Logging in is a session concern (browser profiles, storage state),
  not a recipe one. A recipe may assume an authenticated profile; it may not carry credentials.
- **Pagination across URLs.** A recipe collects from one page. Walking a listing into product
  pages is a crawl, and the crawl frontier already exists.
- **Arbitrary JS.** `execute_js` and `wait_for_function` stay out of the vocabulary (§3).
