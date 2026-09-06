# Recipe contract v2.1 — narrowing the surface, fixing the shape

**Status:** proposal. Amends [`recipe-contract-v2.md`](recipe-contract-v2.md); read that first.

---

## What prompted this

Building the visual picker into the studio (`frontend/src/lib/picker/`) meant
writing a program that *emits* v2 documents rather than a human who writes
them. That is a good stress test of a contract, because a generator cannot
route around a shape it finds awkward — it either produces a valid document or
it does not.

Three things happened, and they are the whole argument for this document:

1. The commonest extraction there is — **N repeating rows already rendered on a
   page** — has no representation. `RepeatSpec` has two kinds and neither is it.
2. `lint.ts` reported the *shipped* `zara-product.v2.json` as broken, on a rule
   about `table` bindings that the contract's own example contradicts. The
   tooling got the data model wrong because the data model keys the same thing
   two different ways.
3. Mapping a picked element to a `Locator` meant filling in 3 of 12 optional
   fields and leaving 9 undefined, with nothing in the type saying which 3.

None of these is "the contract is too big". They are four specific structural
defects, and the proposal is to fix those — plus cut two vocabularies that the
evidence says nobody uses.

---

## What the contract actually offers vs. what the examples use

Measured across all three worked examples in `docs/examples/recipes/` —
**39 fields, 9 groups, 66 candidates**, the corpus the contract was designed
against:

| Vocabulary | Offered | Used | Verdict |
|---|---:|---:|---|
| **Step ops** | 28 | **4** | `wait_for_selector`, `click`, `scroll`, `wait_for_load`. Cut. |
| **Predicate kinds** | 8 | **1** | `visible`, once. Cut. |
| Locator kinds | 7 | 6 | `text` unused; `ax_role` used once. Keep. |
| Transform ops | 25 | **16** | Earning their keep. **Keep.** |
| Assertion kinds | 7 | **6** | Earning their keep. **Keep.** |
| Type kinds | 4 | **4** | All used. **Keep.** |
| `Candidate.when` | — | **0 of 66** | Never used. Cut. |
| `Candidate.variant_id` | — | **28 of 66** | Heavily used. **Keep.** |

**This is the part worth arguing with.** "Simplify the contract" is not
supported by the data as a blanket instruction. Transforms, assertions and the
type system are pulling their weight — 16 of 25 transform ops appear in three
recipes, and the ones that look exotic (`to_object` ×12, `map_lookup` ×3) are
doing real work on real pages. Cutting those would move complexity out of the
document and into either the caller's code or a `lua` block, which is not a
simplification, only a relocation.

What *is* over-provisioned is the **imperative** half: 24 unused step ops and
7 unused predicate kinds, plus a per-candidate guard mechanism with zero uses.
That is where the cutting should happen.

---

## The four structural defects

### D1. `RepeatSpec` cannot express a DOM row set

`RepeatSpec` has exactly two kinds (`recipe/v2/replay.py::_replay_repeat`):

- `json` — iterate an array already in the page's structured data.
- `dom` — **click through an option set**, re-reading the page after each click.

Neither describes a search-results page. Modelling one as a `dom` repeat would
click every card and navigate away on the first. So the picker currently emits
**one `list`-typed field per column**, each read with `all: true`, and the
caller zips them by index.

That works and it is honest, but it has a real failure mode: nothing keeps the
arrays aligned. A card missing a price yields a shorter price column and
silently shifts every value below it. The wizard's preview zips the columns
specifically to make that visible, which is a workaround for a modelling gap,
not a fix.

**Proposed:** a third kind.

```
RepeatSpec
  kind: "dom_rows"
  rows_locator: Locator      # css/xpath matching N row elements
  max_iterations: int
  row_field: str
```

Resolution is `_rows_from_json` with elements instead of array members: resolve
`rows_locator` once, then for each row resolve every column binding *scoped to
that row*. Rows stay aligned by construction, a missing cell is a missing cell
rather than a shift, and the shape the picker naturally produces becomes the
shape the contract naturally wants.

This is the single highest-value change here.

### D2. `within` cannot scope to a row

`evaluate.py` resolves `within` to `containers[0]` — the *first* match, always.
So `within` can scope to a container but never to "the row this column belongs
to", which is precisely what D1 needs and why the column-wise workaround exists
at all.

**Proposed:** under `dom_rows`, column locators resolve against the current row
element rather than `document`. No change to `within`'s meaning elsewhere; the
row is supplied by the iteration, exactly as `_rows_from_json` supplies the
current array member today.

### D3. A field name is a key in three places, and tables use a fourth rule

Today a field appears in `fields`, in `field_groups[].field_names`, **and** in
`field_groups[].bindings` — except for `table` fields, whose `bindings` are
keyed by *column* name instead, with the field name appearing only in
`field_names`.

That inconsistency is not theoretical: `lint.ts` looked for `bindings[name]`
unconditionally and therefore reported every correctly-bound table as
unresolvable, including two of the three errors it raised against the shipped
Zara example. (Fixed in this branch; the fix is what made the rule visible.)

**Proposed:** bindings always key by *what is being bound*, and a table says so
explicitly:

```jsonc
"bindings": {
  "price":    [ /* candidates */ ],              // a scalar field
  "variants": { "size": [...], "sku": [...] }    // a table field, by column
}
```

One rule — "a table's binding is a map of column bindings" — expressed in the
data rather than inferred from `fields[name].type.kind` by every consumer
independently.

### D4. `Locator` is a union pretending to be a struct

12 optional fields spanning 7 kinds, where `selector`, `path`, `role` and
`text` are mutually exclusive but all optional. Nothing in the type stops
`{kind: "css", path: "offers.price"}`, and the JSON Schema's
`additionalProperties: false` does not help because every field is legal
somewhere.

**Proposed:** discriminate properly.

```
Locator = {kind: "css"|"xpath", selector, attribute?, all?, index?, within?, frame?}
        | {kind: "ax_role",     role, name_contains?|name_in?|name_regex?, index?}
        | {kind: "text",        text, index?}
        | {kind: "json_ld"|"hydration"|"meta", path, path_lang?}
```

Same wire format for every document that is valid today. It is a schema and
type-definition change, not a data migration.

---

## The cuts

### C1. Step ops: 28 → 12

Keep what the examples use, plus the ops the authoring UI offers and the ones
whose absence would force a `lua` escape:

```
click · double_click · hover · fill · select_option · scroll · scroll_into_view
wait · wait_for_selector · wait_for_text · wait_for_load · navigate
```

Dropped: `clear`, `press`, `send_keys`, `check`, `uncheck`, `find_text`, `drag`,
`tap`, `swipe`, `wait_for_url`, `dialog_accept`, `dialog_dismiss`, `new_tab`,
`switch_tab`, `close_tab`, `download`.

Several are genuinely useful *interactively* — they exist on the session API and
stay there. The claim is narrower: they have no place in a **replayable
extraction recipe**, which is a document that runs unattended against one page
and returns data. A recipe that manages tabs or accepts dialogs is doing
something the agent path should be doing instead.

### C2. Predicates: 8 → 3, and only on steps

Keep `visible`, `selector_present`, `selector_absent`. Drop `text_present`,
`url_matches`, `json_path_present`, `count_at_least`, `meta_equals` — zero uses
between them.

**Remove `Candidate.when` entirely** (0 of 66 uses). Variant scoping via
`variant_id` — 28 of 66 uses — already covers "this candidate applies to that
page shape", which is the job `when` was there for, and does it with a named,
lintable, page-level concept instead of an inline predicate per candidate.

### C3. `lua` stays, but behind the flag it already has

11 uses across the examples, all doing element-wise work the declarative ops
genuinely cannot express in one pass. Keeping it is right. It should remain
tenant-flagged and `has_script`-marked, and the lint should keep flagging an
unflagged script — which it does.

---

## How the new UI maps onto this

The wizard (`frontend/src/routes/RecipeWizardPage.tsx`) already produces every
construct that survives, and produces nothing that gets cut:

| Wizard step | Emits | After v2.1 |
|---|---|---|
| **Pick** (list) | container + item + per-column chains | one `dom_rows` group — **D1 removes the parallel-array workaround entirely** |
| **Pick** (single field) | ordered `Candidate[]` | unchanged |
| **Fields** | `name`, `TypeSpec`, read `attribute`, chain order | unchanged; D4 makes the locator shape checkable |
| **Reveal** | `click`, `scroll_into_view`, `fill`, `wait_for_selector`, `wait` | all 5 survive C1 |
| **More** | a `click` step, or `scroll` | survives C1 |
| **Preview** | reads via the ported `_READ_JS` | D4 lets the reader switch on `kind` instead of sniffing fields |

The two cut vocabularies are the two the wizard never offered in the first
place, which is its own kind of evidence: building an authoring UI over this
contract independently arrived at roughly the surface this document proposes
keeping.

**What the extension's model contributes.** crawlPilot's recipe is
`{container, item, columns[], pagination}` — nothing else. It cannot express
variants, guarded steps, transform pipelines or assertions, and for the pages it
targets it does not need to. v2's extra machinery is the price of unattended
replay with drift detection, and most of it is worth paying. But the extension
is the proof that the *core* — rows, columns, fallbacks — wants to be one
first-class construct, and `dom_rows` is that construct.

---

## Migration

Every change is additive or a narrowing of what was already unused:

- **D1, D2** — new `RepeatSpec` kind. No existing document changes.
- **D3** — table bindings move from flat to nested. Affects 2 groups across the
  3 examples; `models.py::FieldGroup.from_dict` can accept both shapes for one
  release and emit the nested one.
- **D4** — same wire format, stricter schema. No data change.
- **C1, C2** — no shipped example uses any dropped op or predicate, so no
  migration. Validation should reject them at `POST`/`PUT` with a message
  naming the session API as the alternative.

Order to do them in: **D1 + D2 together** (they are one change and carry all the
value), then D3, then C1/C2 with the schema tightening of D4.

---

## What not to change

- **Transforms (16/25 used).** Cutting these moves work into `lua` or into the
  caller. Neither is simpler.
- **Assertions (6/7 used).** These are what separates a recipe that resolves
  from a recipe that is *correct* — the heal-review gate in
  [`recipe-operations.md`](recipe-operations.md) §D5.2 depends on them.
- **Variants (28/66 candidates).** Doing real work on real pages.
- **The ordered `Candidate[]`.** The single best idea in the contract, and the
  thing the ported picker maps onto most naturally.
- **`outcome: "blocked"` distinct from `failed`.** Non-negotiable, per
  [`recipe-studio.md`](recipe-studio.md).
