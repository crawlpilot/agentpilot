# Authoring: what's built, what's next

Companion to [`recipe-contract-v2.1-proposal.md`](recipe-contract-v2.1-proposal.md).
Answers two open questions and specifies the one piece of backend work the
wizard still needs.

---

## 1. Do we still need the advanced studio editor?

**Yes — but it should stop being a second front door, and it is close to
earning its keep on a much smaller surface.**

The wizard now covers: picking (list and single field), output attribute names,
read attribute, value types, fallback-chain ordering with test-on-page, reveal
steps with the global-setup boundary made explicit, pagination, regex cleanup,
a row-wise preview, and JSON output. That is the whole of an ordinary recipe.

What is left only in the editor, measured against the three shipped examples:

| Capability | Used | Could it move to the wizard? |
|---|---|---|
| **Variants** + detect predicates | **28 of 66 candidates** | Not cheaply. A variant is a claim about *page shapes*, not about one field — it needs its own surface. |
| **Assertions** | 6 of 7 kinds, 20 uses | Yes, and it should. `not_empty` and `range` on a field row would catch most of it. |
| Per-step `retry`, `timeout_ms`, `when` | a handful | No. Rare, and each needs explaining. |
| `to_object`, `map_lookup`, `json_path`, `lua` | 27 uses | No. These are discovered from a failed *run*, not while picking. |
| Raw JSON edit | — | Already in the wizard's Review step. |

So the honest answer is that the editor's unique value has narrowed to
**variants, exotic transforms, and per-step execution control** — a real slice,
needed by the hard pages the contract was designed against, and not worth
rebuilding inside a five-step flow.

**Recommendation:** keep it, and make the demotion explicit.

- The wizard is the only entry point for a new recipe (already true —
  `RecipesListPage` has one button).
- The editor is reachable from the wizard and from a recipe's detail page, and
  is framed as "the rest of the contract", not as an alternative way to do the
  same thing.
- Move **assertions** into the wizard's field row next; that is the one item
  above with a clear yes.
- Revisit after the editor has been in this position for a while. If variants
  turn out to be the *only* reason anyone opens it, fold a variant picker into
  the wizard and retire it.

Deleting it now would strand the Zara/Walmart class of page with no authoring
surface at all, to save a screen nobody is forced to look at.

---

## 2. Saving a recipe

**Not built. It needs backend work that does not exist yet**, and the wizard
currently ends at "download the JSON / open in the advanced editor".

### What is missing

`POST /v1/recipes` takes the **v1** shape (`{name, url, field_schema,
schedule_interval_seconds}` — `gateway/schemas.py`) and immediately queues an
agent *build* run. There is no `PUT`, and no way to submit a document that has
already been authored. So today a hand-authored v2 recipe cannot be persisted
at all, which is the blocker `recipe-studio.md` named from the start.

### Proposed

```
POST /v1/recipes/v2        {recipe: <v2 document>}  -> {recipe_id, version}
PUT  /v1/recipes/{id}      {recipe: <v2 document>}  -> {version}
```

Both write a `recipe_versions` row exactly as build and heal already do —
`recipe_versions` is append-only, so an edit is a new version and rollback
stays possible. Neither queues a run: a document authored by hand has already
been previewed against a live page, and silently starting a build would
overwrite it with the agent's answer.

Validation on the way in should be the same lint the studio runs, server-side,
rejecting on `error` and returning `warning`s in the response body — so the two
cannot drift.

### Then, in the wizard

The Review step gains **Save recipe** (primary) beside the existing JSON
download, and reports the version it wrote. `useRecipeDoc.markSaved` already
exists for exactly this and is currently unused.

### Order

This is the highest-value remaining work. Everything else in the wizard
produces a document nobody can keep.

---

## 3. Done in this round

- **Regex cleanup** — `CleanupEditor` on every field and every table column:
  trim, collapse whitespace, extract/replace by pattern, strip HTML, cast. It
  previews the effect on the value the picker actually read, labelled as an
  approximation because the real transforms run server-side.
- **JSON output preview** — the Preview step has a **JSON output** view showing
  the payload a caller receives, built by `toOutputJson`. A field that did not
  resolve is *absent*, matching `replay.py::_record` rather than emitting null.
- **Global setup made visible** — actions above the first field compile into
  `global_setup`; the list now says so, and marks where per-group steps begin.
  The distinction is load-bearing (global setup re-runs for every group, after
  navigation) and was previously invisible.
- **`dom_rows` end to end** — a picked list is now one `table` field with a
  `dom_rows` repeat, columns resolved per row. The parallel-array workaround
  and its silent-misalignment hazard are gone from both halves.
