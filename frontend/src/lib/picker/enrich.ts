/**
 * Fills the two gaps between what the extension's picker emits and what a v2
 * recipe needs. Runs in the page, immediately after a selection, while the DOM
 * that produced it is still live -- which is the only moment either question
 * can be answered.
 *
 * Neither gap is a defect upstream. They are places where the extension's own
 * execution model made the work unnecessary:
 *
 * 1. **Detail picks lose their candidate chain.** `DetailSelectionStrategy`
 *    computes the full ranked list via `generateRobustSelectors`, then keeps
 *    only `bestCss` and `bestXPath` and drops the rest. The extension resolves
 *    a field by trying its one selector; v2 resolves a field by walking an
 *    *ordered `Candidate[]` until one yields* -- the fallback chain is the
 *    entire resilience model. So the chain is regenerated here.
 *
 * 2. **List columns have no locator.** `SchemaGenerator` gives every column a
 *    `selector` like `"prod > h3"`, which is a human-readable *path key* from
 *    `generateNodeName`, not a CSS selector -- the extension re-walks each row
 *    with `DataExtractor` and matches columns by that key, so it never needs
 *    one. A v2 binding must address the value directly, relative to its row.
 *
 * 3. **A detail pick that lands on a repeating container has no row
 *    selector.** `DetailSelectionStrategy` runs the container detector too, and
 *    on a hit it classifies the pick `list`/`table` and extracts every row into
 *    `data` -- but the selectors it emits address the *container* and the
 *    *clicked element*, never the rows. The extension does not need one: at
 *    extraction time it re-runs `DataExtractor` over the container's children
 *    in the page (`ElementProcessor::extractContainerRows`). A v2 recipe has no
 *    such second visit; `dom_rows` needs a `rows_locator` written down now.
 *
 * The second is solved by matching values rather than replaying the walk: take
 * what the extractor read for a column, find the descendant of the first row
 * that actually holds it, and generate a row-relative selector for that
 * element. This is the same tactic the studio's existing JSON probe uses to
 * answer "where does this value live?" (`lib/recipe/probe.ts`), and it is
 * robust to the extractor's internal pathing changing.
 *
 * The third is solved by re-running the *same* detector over the container the
 * strategy already found, which hands back the same row elements it extracted
 * from, and deriving a row selector from them with `CommonSelectorGenerator` --
 * exactly what `ListSelectionStrategy` does for a list-mode pick. The result is
 * that a detail-mode container pick reaches `fromPick.ts` in the same shape as
 * a list-mode one, and one code path serves both.
 */
import { generateRobustSelectors, generateXPath } from './vendor/content/services/dom/domUtils'
import { ContainerDetector } from './vendor/content/features/picker/ContainerDetector'
import { CommonSelectorGenerator } from './vendor/content/services/dom/selectors/CommonSelectorGenerator'
import { isStableAttributeValue } from './vendor/shared/selectors/stability'
import type { PickMessage, PickPayload, PickColumn, PickSelector } from './protocol'

/** Attributes worth reading instead of text, keyed by inferred column type. */
const ATTRIBUTE_FOR_TYPE: Partial<Record<PickColumn['type'], string>> = {
  url: 'href',
  image: 'src',
  image_array: 'src',
}

function textOf(el: Element): string {
  return (el.textContent ?? '').trim()
}

/** The value an element would contribute for a column of this type. */
function valueOf(el: Element, attribute?: string): string {
  if (!attribute) return textOf(el)
  // Read the *resolved* property, not the literal attribute: the extractor
  // normalises URLs, so `/a` was recorded as `https://host/a` and comparing
  // against `getAttribute('href')` would never match.
  if (attribute === 'href' && el instanceof HTMLAnchorElement) return el.href
  if (attribute === 'src' && el instanceof HTMLImageElement) return el.src
  return el.getAttribute(attribute) ?? ''
}

/**
 * The descendant of `row` holding `value`, preferring the most deeply nested
 * match. A card's text also appears on the card itself and on every wrapper
 * between; the innermost element is the one an author means, and the one whose
 * selector stays meaningful when the layout shifts.
 */
function findValueHolder(row: Element, value: string, attribute?: string): Element | null {
  if (!value) return null
  const candidates = attribute
    ? Array.from(row.querySelectorAll(attribute === 'href' ? 'a[href]' : 'img[src]'))
    : Array.from(row.querySelectorAll('*'))

  let best: Element | null = null
  let bestDepth = -1
  for (const el of candidates) {
    if (valueOf(el, attribute) !== value) continue
    let depth = 0
    for (let p = el.parentElement; p && p !== row; p = p.parentElement) depth++
    if (depth > bestDepth) {
      best = el
      bestDepth = depth
    }
  }
  return best
}

/**
 * Reject a selector that embeds a value which differs from row to row.
 *
 * The generators happily produce things like `[src="/1.jpg"]` -- perfectly
 * unique, and useless as a *column* selector, because it identifies one row's
 * image rather than "the image in this row". It resolves for row one and
 * matches nothing in the rest, so the column would look fine in a preview of
 * the first row and be empty everywhere below it.
 *
 * `isStableAttributeValue` already encodes which attributes are instance
 * -specific (`src`, `href`, `id`, `data-id`, ...); this applies that judgement
 * to a generated selector string.
 */
function embedsVolatileValue(selector: string): boolean {
  // [attr="value"] / [attr='value'] / [attr=value]
  const pattern = /\[\s*([\w-]+)\s*=\s*("([^"]*)"|'([^']*)'|([^\]]*))\s*\]/g
  for (const match of selector.matchAll(pattern)) {
    const attr = match[1]
    const value = match[3] ?? match[4] ?? match[5] ?? ''
    if (!isStableAttributeValue(attr, value.replace(/\\/g, ''))) return true
  }
  return false
}

function toPickSelectors(results: { selector: string; strategy: string }[]): PickSelector[] {
  const seen = new Set<string>()
  const out: PickSelector[] = []
  for (const r of results) {
    if (!r.selector || seen.has(r.selector)) continue
    seen.add(r.selector)
    out.push({ selector: r.selector, strategy: r.strategy })
  }
  return out
}

/** Resolve the rows the pick refers to, so columns can be located inside one. */
function rowsOf(payload: PickPayload): Element[] {
  const { containerSelector, itemSelector } = payload
  if (!itemSelector) return []
  try {
    const container = containerSelector ? document.querySelector(containerSelector) : document
    if (!container) return []
    return Array.from(container.querySelectorAll(itemSelector))
  } catch {
    return []
  }
}

function queryOne(selector: string | undefined): HTMLElement | null {
  if (!selector) return null
  try {
    return document.querySelector<HTMLElement>(selector)
  } catch {
    return null
  }
}

/**
 * Is this a specification table -- rows of *label -> value* rather than rows of
 * comparable records?
 *
 * The distinction decides the output shape, and it is not one the column
 * inference can make: both come back as a 2-column table, and both are
 * perfectly good tables. The difference is what a caller wants at the end --
 * `{"Brand": "Nike", "Colour": "Red"}` for one, a list of row objects for the
 * other -- and getting it wrong produces a technically-correct result nobody
 * asked for.
 *
 * So this only claims the cases where the *markup itself* says "label": a `th`
 * or a `dt` leading the row. Those two elements mean exactly this and nothing
 * else, which makes them the only evidence worth acting on without asking.
 * Every other spec table -- and there are many, all `td` -- is offered as a
 * toggle in the wizard instead of guessed at.
 */
function looksKeyValue(rows: Element[]): boolean {
  if (rows.length < 2) return false
  const labelled = rows.filter((row) => {
    const cells = Array.from(row.children).filter((c) => c.tagName !== 'BR' && c.tagName !== 'HR')
    return cells.length === 2 && (cells[0].tagName === 'TH' || cells[0].tagName === 'DT')
  })
  return labelled.length >= rows.length * 0.8
}

function enrichColumns(payload: PickPayload): PickPayload {
  const columns = payload.data?.columns
  const firstItem = payload.data?.items?.[0]
  if (!columns?.length || !firstItem) return payload

  const rows = rowsOf(payload)
  const row = rows[0]
  if (!row) return payload

  const keyValue = columns.length === 2 && looksKeyValue(rows)

  const enriched = columns.map((col) => {
    const attribute = ATTRIBUTE_FOR_TYPE[col.type]
    const raw = firstItem[col.id]
    if (typeof raw !== 'string' || !raw) return col

    // An image_array column stores a JSON array; any one of its members
    // identifies the element whose *repeated* selector collects them all.
    let value = raw
    if (col.type === 'image_array') {
      try {
        const parsed = JSON.parse(raw)
        if (Array.isArray(parsed) && typeof parsed[0] === 'string') value = parsed[0]
      } catch {
        // Not JSON after all -- fall through and match the raw string.
      }
    }

    const el = findValueHolder(row, value, attribute)
    if (!el) return col

    const results = generateRobustSelectors(el as HTMLElement, { root: row as HTMLElement, isList: true })
    // A column selector must address "this cell in any row", so anything
    // carrying one row's own value is worse than useless here.
    const usable = toPickSelectors(results).filter((s) => !embedsVolatileValue(s.selector))
    const locators = usable.length > 0 ? usable : toPickSelectors(results)
    if (locators.length === 0) return col

    return {
      ...col,
      // `selector` stays the extractor's path key; the studio reads `locators`.
      locators,
      attribute,
      xpath: generateXPath(el as HTMLElement),
    }
  })

  return { ...payload, keyValue, data: { ...payload.data!, columns: enriched } }
}

/** The detail classifications that mean "a repeating set", not "one value". */
const CONTAINER_TYPES = new Set(['list', 'table'])

/**
 * A detail pick that landed on a repeating container is a list pick that came
 * through a different door.
 *
 * `DetailSelectionStrategy` already ran the container detector, already
 * classified the pick `list`/`table`, and already extracted every row into
 * `data`. What it did not do is write down how to *reach* a row: it emits the
 * container and the clicked element, because the extension re-derives the rows
 * in the page at extraction time and never needs a selector for them.
 *
 * A v2 recipe does. Without one there is no `rows_locator`, so no `dom_rows`
 * repeat, so the whole table collapses to a single scalar read of the
 * container -- one blob of concatenated cell text where a caller asked for
 * rows. Re-running the detector over the container the strategy found returns
 * the same row elements it extracted from, and `CommonSelectorGenerator` turns
 * those into the same ranked, majority-validated chain a list-mode pick gets.
 */
function enrichDetailContainer(payload: PickPayload): PickPayload {
  const clicked = queryOne(payload.containerSelector)
  if (!clicked) return payload

  // The same detector call the strategy made, over the container it settled
  // on -- so the rows here are the rows `data` was extracted from, not a
  // second opinion about what the rows are.
  const info = new ContainerDetector().findInternalContainer(clicked)
  if (!info.isContainer || !info.container || info.siblings.length === 0) return payload

  const rows = toPickSelectors(
    new CommonSelectorGenerator()
      .deriveCommonSelector(info.siblings, info.container, info.siblings[0])
      .map((d) => ({ selector: d.selector, strategy: `Deductive ${d.type}` })),
  )
  if (rows.length === 0) return payload

  let next: PickPayload = {
    ...payload,
    // Row selectors are derived relative to the container, so both have to
    // move together if the detector settled somewhere other than the clicked
    // element -- a container/row pair from two different scopes resolves to
    // nothing.
    ...(info.container === clicked
      ? {}
      : {
          containerSelector: generateRobustSelectors(info.container)[0]?.selector ?? payload.containerSelector,
          containerSelectors: toPickSelectors(generateRobustSelectors(info.container)),
        }),
    itemSelector: rows[0].selector,
    itemSelectors: rows,
    itemXPath: generateXPath(info.siblings[0]),
    patternFound: true,
    count: payload.count || info.siblings.length,
  }
  next = enrichColumns(next)
  return next
}

function enrichDetail(payload: PickPayload): PickPayload {
  const withChain = withDetailChain(payload)
  return CONTAINER_TYPES.has(withChain.extractionType ?? '')
    ? enrichDetailContainer(withChain)
    : withChain
}

function withDetailChain(payload: PickPayload): PickPayload {
  if (payload.itemSelectors?.length) return payload

  const el = queryOne(payload.containerSelector)
  if (!el) return payload

  const chain = toPickSelectors(generateRobustSelectors(el))
  if (chain.length === 0) return payload
  return { ...payload, itemSelectors: chain, containerSelectors: chain }
}

/** Enrich a selection in place of the raw upstream payload. */
export function enrich(msg: PickMessage): PickMessage {
  if (msg.type !== 'ELEMENT_SELECTED' || !msg.payload) return msg
  try {
    const payload =
      msg.payload.selectionMode === 'list' ? enrichColumns(msg.payload) : enrichDetail(msg.payload)
    return { ...msg, payload }
  } catch {
    // Enrichment is an improvement, never a gate. A page that defeats it still
    // yields the upstream payload, and the studio still gets a usable pick.
    return msg
  }
}
