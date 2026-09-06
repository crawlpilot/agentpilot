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
 * The second is solved by matching values rather than replaying the walk: take
 * what the extractor read for a column, find the descendant of the first row
 * that actually holds it, and generate a row-relative selector for that
 * element. This is the same tactic the studio's existing JSON probe uses to
 * answer "where does this value live?" (`lib/recipe/probe.ts`), and it is
 * robust to the extractor's internal pathing changing.
 */
import { generateRobustSelectors, generateXPath } from './vendor/content/services/dom/domUtils'
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

/** Resolve the first row the pick refers to, so columns can be located in it. */
function firstRow(payload: PickPayload): Element | null {
  const { containerSelector, itemSelector } = payload
  if (!itemSelector) return null
  try {
    const container = containerSelector ? document.querySelector(containerSelector) : document
    if (!container) return null
    return container.querySelector(itemSelector)
  } catch {
    return null
  }
}

function enrichList(payload: PickPayload): PickPayload {
  const columns = payload.data?.columns
  const firstItem = payload.data?.items?.[0]
  if (!columns?.length || !firstItem) return payload

  const row = firstRow(payload)
  if (!row) return payload

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

  return { ...payload, data: { ...payload.data!, columns: enriched } }
}

function enrichDetail(payload: PickPayload): PickPayload {
  if (payload.itemSelectors?.length) return payload

  let el: Element | null = null
  try {
    el = payload.containerSelector ? document.querySelector(payload.containerSelector) : null
  } catch {
    el = null
  }
  if (!el) return payload

  const chain = toPickSelectors(generateRobustSelectors(el as HTMLElement))
  if (chain.length === 0) return payload
  return { ...payload, itemSelectors: chain, containerSelectors: chain }
}

/** Enrich a selection in place of the raw upstream payload. */
export function enrich(msg: PickMessage): PickMessage {
  if (msg.type !== 'ELEMENT_SELECTED' || !msg.payload) return msg
  try {
    const payload =
      msg.payload.selectionMode === 'list' ? enrichList(msg.payload) : enrichDetail(msg.payload)
    return { ...msg, payload }
  } catch {
    // Enrichment is an improvement, never a gate. A page that defeats it still
    // yields the upstream payload, and the studio still gets a usable pick.
    return msg
  }
}
