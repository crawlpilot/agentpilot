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

**Built.** This section described it as missing; it shipped and the description
went stale, which cost a later investigation real time. What exists now:

```
POST /v1/recipes/v2        {recipe: <v2 document>}  -> {recipe_id, version, warnings}
PUT  /v1/recipes/{id}      {recipe: <v2 document>}  -> {recipe_id, version, warnings}
```

Both are in `gateway/routes/recipes.py` (`save_recipe_v2`, `update_recipe`) and
share `_save`, so create and update cannot diverge. Neither queues a run: a
document authored by hand has already been previewed against a live page, and
silently starting a build would overwrite it with the agent's answer. Each write
appends a `recipe_versions` row exactly as build and heal do, so an edit is a new
version and rollback stays possible.

Validation on the way in is `recipe/v2/validate.py::validate_document` — the same
lint the studio runs, server-side — rejecting on `error` with **422** and
returning `warning`s in the response body.

### The one thing that was wrong for a long time

`_save` puts its reasons in `HTTPException.detail` as
`{"errors": [...], "warnings": [...]}`, and the gateway's handler ran
`str(exc.detail)` over it. So the `error` field carried a Python dict repr and
`details` carried nothing, which in the studio made a *refused* save look like a
*broken* save — the author had to read a Python literal to discover that, for
instance, a `dom_rows` repeat was missing its `rows_locator`.

Fixed: a mapping detail now travels whole in `details`, with `error` carrying a
readable summary (`errors.py::_summarize_detail`). The wizard joins the full list
through `validationErrors` in `lib/api/client.ts`, so every reason is shown at
once rather than one per save round trip.

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
