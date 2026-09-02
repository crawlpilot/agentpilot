# Recipe Studio

**Status:** specification. The authoring surface for
[`recipe-contract-v2.md`](recipe-contract-v2.md).

---

## Why a separate surface

The recipe UI today is a form over the API.
[`RecipeCreateDialog.tsx`](../frontend/src/components/app/RecipeCreateDialog.tsx) collects a name,
a URL, a schedule interval, and **the entire field schema as raw JSON in a textarea**.
[`RecipeFieldGroupsList.tsx`](../frontend/src/components/app/RecipeFieldGroupsList.tsx) renders
the result as a read-only `JSON.stringify` dump.

A v2 recipe is a structured document with ordered candidate lists, guarded steps, variant
predicates, transform pipelines and per-field assertions. The three example recipes in
`docs/examples/recipes/` run to 39 fields, 9 groups and 66 candidates between them. A textarea
cannot author that and a `<pre>` cannot review it.

There is also a hard blocker: **there is no update endpoint.** `POST /v1/recipes` is the only
ingress, so nothing can be hand-edited or repaired after the agent builds it. Today a recipe with
one wrong selector must be rebuilt from scratch.

And there is a reason beyond ergonomics. Recipes are authored by a model reading an untrusted
page, and — per [`recipe-operations.md`](recipe-operations.md) §D3 — LLM first-attempt selectors
fail to yield data 30–40% of the time. **Review is the control that makes agent-authored recipes
trustworthy.** The studio is where `draft` becomes `approved`; without somewhere to look at the
work, that gate is a rubber stamp.

---

## Routes

```
/recipes/new                     the build wizard
/recipes/:recipeId/studio        the editor
```

Both render **full-bleed, outside the `App` shell** — the precedent is
`/sessions/:sessionId/live`, already registered outside the sidebar layout in
[`router.tsx`](../frontend/src/router.tsx). A studio needs the width.

`/recipes` and `/recipes/:recipeId` keep their current list/detail role; the detail page gains an
**Open in studio** action and a health panel.

---

## Layout: three panes

### Left — Output schema

The caller's data contract, edited as a field tree rather than raw JSON. Per field: name,
`TypeSpec` (scalar / list / object / table), `value_type`, `required`, `emit_raw`, description,
and its **assertions**.

This pane comes first because it is the input the build agent works from. The `description` is
read by the model, so the editor labels it as such — "written for a reader" is a design note, not
a placeholder.

`object` with no declared properties is offered explicitly as **"open map — keys come from the
page"**, since that is what a spec sheet actually is, and it is the shape the Amazon and Walmart
examples use for `specifications`.

### Centre — The live page

Reuses [`LiveViewCanvas`](../frontend/src/components/app/LiveViewCanvas.tsx) /
[`LiveViewPanel`](../frontend/src/components/app/LiveViewPanel.tsx) (CDP screencast, already
built) with [`SnapshotTree`](../frontend/src/components/app/SnapshotTree.tsx) beside it.

**Click an element → ranked candidate locators appear.** The ranking already exists server-side
in `recipe/locator_proposal.py` and `recipe/selector_synthesis.py`; it is simply unreachable from
outside a build run. Assign a candidate to a field in one click.

Three affordances that the real pages made obviously necessary:

- **A source badge on every proposed candidate** — `json_ld` / `hydration` / `meta` / `css` /
  `xpath` / `ax_role` — with structured-data candidates sorted first. On Walmart the *right*
  answer for six sections is a hydration path, and a picker that only proposes CSS would lead
  every author to the wrong one.
- **A "this data is also in the JSON" hint.** When a clicked element's text also appears in the
  page's structured data, say so and offer that path instead. This is the single highest-value
  nudge the studio can give: it is the difference between the Walmart recipe costing one read and
  costing six clicks.
- **A collapsed-content indicator.** An element that is in the DOM but not rendered is readable
  via `text` (textContent) with no click at all — see contract §2. The picker must distinguish
  "not there" from "there but not painted", because on Amazon four sections are the latter and
  the naive reading is a needless reveal step.

### Right — The recipe document

Tabs:

| Tab | Contents |
|---|---|
| **Steps** | `global_setup` and per-group step lists, drag to reorder. Each row: op, target, `timeout_ms`, `on_error`, `when`. Reuses the [`PlaygroundActionRow`](../frontend/src/components/app/PlaygroundActionRow.tsx) pattern. |
| **Fields** | Per field, the ordered candidate list. **Drag to reorder priority.** Badges for `variant_id`, `when`, `verified_on`. A live "resolves to →" preview against the current page. |
| **Transforms** | Pipeline builder per field with before/after preview on the live value. The Lua editor appears only under the tenant flag. |
| **Variants** | Variant list with detect predicates, plus a "which variant matches this URL?" tester. |
| **Quality** | Assertions per field, `expect.min_rows` per group, and the trailing metrics from `recipe_field_metrics`. |
| **JSON** | The raw document, always in sync, exportable. The escape hatch for power users, and the thing you paste into a bug report. |

Two lints worth building in, because both are mistakes the real pages invite:

- **`wait` where a `wait_for_*` would do.** A fixed sleep is always the weaker choice when a
  condition is available.
- **A candidate with `verified_on: 0`** on an `approved` recipe. It means nobody has ever seen it
  work.

---

## Two authoring modes

### Agent-authored

Enter **sample URLs — plural** (the field is a list, and the form says why: one URL yields an
overfit guess, per [`recipe-operations.md`](recipe-operations.md) §D4.1) and a schema, then
**Build with agent**, and watch the exploration live.

The plumbing exists: agent runs already stream over SSE at `GET /v1/agent/runs/{id}/events` with
per-step screenshots, and [`AgentRunView`](../frontend/src/components/app/AgentRunView.tsx) /
[`AgentActionCard`](../frontend/src/components/app/AgentActionCard.tsx) already render it. The
produced recipe lands in the editor as `draft`.

### Human-authored and repaired

Pick elements by hand. Fix one broken selector without re-running a build. Reorder candidates
when the primary starts losing. This is the mode that the missing `PUT` endpoint currently makes
impossible, and it is the cheap path for most real maintenance.

---

## The preview bar

Run against an arbitrary `{url, metadata}` and render the contract's §9 result model:

- `field_status` chips — **resolved** (green) · **fallback** (blue) · **suspect** (amber) ·
  **empty** (grey) · **failed** (red)
- `provenance` per field: which candidate index, which source, which variant
- `truncated` warnings on table fields
- assertion results
- `step_trace` with per-step timings

`outcome: "blocked"` gets its own unmistakable banner — **not** an error state. Confusing those
two is the exact failure this whole design is trying to prevent, and the UI should not reintroduce
it. A blocked preview says "we could not see the page", never "your recipe is broken".

The metadata box is a JSON editor, because `{{meta.*}}` templating (contract §1) is only testable
if you can vary the metadata.

---

## Heal review

A heal produces a **new version**, never an in-place edit. The studio shows a **side-by-side diff
of the two versions *and their extracted values* on the same sample URLs**, with changed values
highlighted.

This is the gate from [`recipe-operations.md`](recipe-operations.md) §D5.2. A heal that "fixed"
`price` by repointing it at a sponsored ad's price resolves cleanly and looks healthy in every
automated check; it is obvious in a value diff. **Promote** and **Reject** are the two actions,
and rejection rolls back to the previous version — `recipe_versions` is already append-only, so
the data is there.

---

## Endpoints the studio requires that do not exist

| Endpoint | Why |
|---|---|
| `PUT /v1/recipes/{id}` | **No update endpoint exists today.** Must write a `recipe_versions` row, exactly as build and heal do. |
| `POST /v1/recipes/{id}/dry-run` `{url, metadata}` | Synchronous replay for the preview bar. Today only the async `POST /run` exists, which is wrong for an interactive loop. |
| `POST /v1/recipes/{id}/validate` | Schema + locator lint with no browser. Fast feedback while typing. |
| `POST /v1/sessions/{id}/propose-locators` `{ref \| selector}` | Exposes `locator_proposal` / `selector_synthesis` for click-to-pick. |
| `POST /v1/recipes/{id}/versions/{v}/promote` | `draft → approved → published`, and rollback. |
| `GET /v1/recipes/{id}/metrics` | The trailing `recipe_field_metrics` for the Quality tab. |

All are CRUD-over-Postgres except `dry-run` and `propose-locators`, which need a browser and so
follow the existing worker `/internal/…` + gateway `_proxy` pattern (see
`gateway/routes/scrape.py` vs `scrape_proxy.py`).

---

## Stack

Already set, and to be followed rather than re-litigated: React 19, Vite 8, Tailwind 4, Radix
primitives, TanStack Query, react-router 7. shadcn-style primitives in `src/components/ui/`,
domain components in `src/components/app/`, pages in `src/routes/`, query hooks in `src/hooks/`,
the typed client in `src/lib/api/`. Lint `oxlint`; build `tsc -b && vite build`.

Drag-and-drop is the one genuinely new interaction primitive (candidate priority, step order).
Nothing in the current dependency set provides it; prefer the smallest option that handles a
vertical list with keyboard accessibility over pulling in a general DnD framework.

---

## Deleted, not adapted

`RecipeCreateDialog.tsx` and `RecipeFieldGroupsList.tsx` are superseded. `useRecipes.ts` and
`lib/api/recipes.ts` are rewritten against the v2 shapes and the six endpoints above. See
[`recipe-contract-v2-cleanup.md`](recipe-contract-v2-cleanup.md).
