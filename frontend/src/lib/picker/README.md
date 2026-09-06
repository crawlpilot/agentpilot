# The visual picker

Click-to-pick over a live browser session. `vendor/` is ported from the
[crawlPilot browser extension][upstream]; everything beside it is the adapter
that makes it work without an extension runtime and turns what it emits into a
v2 recipe.

**Upstream:** `crawlpilot/crawlpilot` @ `a9355a8` (`release-2.0.0`, 2026-07-14),
paths under `src/content/**` and `src/shared/**`.

## How it runs

There is no new transport and no backend change.

```
studio (React)                              remote page (CDP session)
──────────────                              ─────────────────────────
usePagePicker.pick('list')
  └─ execute_js: <bundle>; __cpPicker.start ──▶ entry.ts installs, draws overlay
                                                        │
     the author sees the overlay ◀── CDP screencast ─────┤
     the author's mouse ──── live view `interact` ──────▶ real Input.* events
                                                        │
                                                 strategy.handleClick
                                                 enrich() → window.__cpPickResult
  ◀─ execute_js: __cpPicker.take() (polled) ──────────────┘
     fromPick.ts → Candidate[] / FieldSpec
```

Three properties of the existing live view carry the whole design: it is a
screencast of the *real* page, `interact` mode dispatches real input into it,
and `execute_js` already returns values. So the picker draws where the author
is already looking, is clicked by input that already reaches it, and answers
through a channel that already exists.

The one thing CDP does not give us free is the *return* path — `execute_js` is
request/response and a click happens whenever it happens. So the page parks the
payload on a global and `usePagePicker` polls a destructive `take()`. If that
latency ever matters, the fix is a `Runtime.addBinding` push over the live-view
WebSocket, not a shorter interval.

## Files

| File | Role |
|---|---|
| `vendor/**` | Ported verbatim. Overlay, container detection, selector generation, extraction, stability heuristics. ~5.2k lines. |
| `protocol.ts` | The contract between the two halves. No runtime imports — both bundles include it. |
| `entry.ts` | The in-page API installed on `window.__cpPicker`. Built alone into `generated/picker.iife.js`. |
| `enrich.ts` | Fills the two gaps between what upstream emits and what v2 needs (below). |
| `generated/picker.iife.js` | **Committed build output.** Regenerate with `npm run build:picker`. |
| `../../hooks/usePagePicker.ts` | Injection, polling, cancel, refine, selector test. |
| `../recipe/fromPick.ts` | Pick → `Candidate[]` / `FieldSpec` / field drafts. |

## Divergences from upstream

Three, all deliberate, all documented at their site:

1. **`VisualElementPicker.ts` — the `chrome.*` seam.** Upstream delivered a
   finished selection via `chrome.runtime.sendMessage` in four places. There is
   no extension runtime here, so those became one `emit` sink that `entry.ts`
   points at `window.__cpPickResult`. This is the *only* edit inside `vendor/`
   beyond mechanical `import type` conversions for `verbatimModuleSyntax`.

2. **`enrich.ts` — detail picks lose their candidate chain.**
   `DetailSelectionStrategy` computes the full ranked list, keeps only the best
   CSS and best XPath, and drops the rest. The extension resolves a field by
   trying its one selector; v2 walks an *ordered chain until one yields*. The
   chain is regenerated rather than upstream being changed.

3. **`enrich.ts` — list columns have no locator.** `SchemaGenerator` gives each
   column a `selector` like `"prod > h3"`, which is a human-readable path key,
   not CSS — the extension re-walks every row and matches by that key, so it
   never needs one. `enrich` finds the element actually holding each column's
   value and generates a row-relative selector for it.

## Re-syncing

Keep `vendor/` a copy, not a fork. To pull upstream changes: re-copy the tree,
re-apply divergence 1, re-run the `import type` conversions, then
`npm run build:picker && npm test`. The four upstream test suites came across
unchanged and are the regression net — if a re-sync alters the selector or
schema heuristics, they fail.

`vendor/` and `generated/` are both excluded from `oxlint` (`.oxlintrc.json`):
findings in vendored code are upstream's to fix, and diverging to satisfy a
local rule turns the next re-sync into a manual merge.

## Known limit: repeating DOM lists

A picked list becomes **one `list`-typed field per column**, each read with
`all: true`, rather than one `table` field of rows.

This is forced by the replay engine, not preferred. A `table` field's rows come
only from `RepeatSpec` (`recipe/v2/replay.py::_replay_repeat`), which has two
kinds: `json`, iterating an array already in the page's structured data, and
`dom`, which *clicks through an option set*. Neither describes "N cards already
rendered on a search page" — and modelling it as a `dom` repeat would click
every card, navigating away on the first.

The cost is real: nothing keeps the parallel arrays aligned. A card missing a
price yields a shorter price list and silently shifts every value after it. The
fix is a `dom_rows` repeat kind — `rows_locator` over DOM nodes, columns
resolved per row, exactly `_rows_from_json` but against elements — which is a
backend change and deliberately outside this port's scope.

[upstream]: https://github.com/crawlpilot/crawlpilot
