// Reading a page's embedded structured data, and finding the path to a value.
//
// This exists because of one repeated finding across the worked examples: the
// value an author is about to write a CSS selector for is very often already
// sitting in JSON-LD, a hydration blob or a meta tag -- where it survives a
// redesign that renames every class. On Walmart the right answer for six
// sections is a hydration path; a picker that only proposes CSS leads every
// author to the wrong one.
//
// So the flow is inverted from a normal element picker: you paste the value
// you can see, and this says where it already lives.

import type { Locator, LocatorKind, PathLang } from './types'

/**
 * The structured-data read, as the *engine* performs it.
 *
 * This was a hand-written `execute_js` snippet that walked the DOM itself, and
 * that was the bug. Replay does not run it -- `PageReader.structured_data()`
 * issues `ExtractAction(format="structured_data")`, which is
 * `extraction/structured_data.py`, and the two disagreed about the shape of
 * every container:
 *
 * - **`json_ld` is flattened server-side.** `_flatten_json_ld` spreads a
 *   top-level array and unwraps `@graph` into a flat list of entities. The
 *   snippet pushed each `<script>`'s parsed body whole, so on any page using
 *   `@graph` -- Yoast, Shopify, most CMSs -- the studio offered
 *   `[0].@graph[2].name` for a value replay reads at `[2].name`. Every such
 *   path resolved to nothing at run time.
 * - **`metadata` is normalised.** The extractor derives `title`, `language`
 *   and `favicon`, and turns duplicate keys into lists (joining
 *   `description`-like ones). The snippet kept last-wins strings and had none
 *   of the derived keys.
 * - **`hydration` keys differed outright**: the snippet read
 *   `__PRELOADED_STATE__`, which the extractor does not, and missed
 *   `__REDUX_STATE__`, which it does.
 *
 * Reading through the same action removes the class of bug rather than this
 * instance of it: there is now one definition of where a value lives, so a
 * path the studio proposes is a path replay can resolve by construction.
 */
export const STRUCTURED_DATA_ACTION = { type: 'extract', format: 'structured_data' } as const

/** Exactly `extract_structured_data`'s return shape. */
export interface ProbeResult {
  json_ld: unknown[]
  /** Named `metadata` server-side, which is what `_SOURCE_TO_CONTAINER` maps
   * the `meta` locator kind onto. Keeping the server's name here is what stops
   * the two drifting again. */
  metadata: Record<string, unknown>
  hydration: Record<string, unknown>
}

/** Parse one `extracts[0]` payload, tolerating a page that yielded nothing. */
export function parseProbe(raw: unknown): ProbeResult {
  const empty: ProbeResult = { json_ld: [], metadata: {}, hydration: {} }
  if (typeof raw !== 'string') return empty
  try {
    const parsed = JSON.parse(raw) as Partial<ProbeResult>
    return {
      json_ld: Array.isArray(parsed.json_ld) ? parsed.json_ld : [],
      metadata: parsed.metadata ?? {},
      hydration: parsed.hydration ?? {},
    }
  } catch {
    return empty
  }
}

export interface PathHit {
  kind: LocatorKind
  /** Dotted/bracket path in the contract's `simple` dialect. */
  path: string
  value: string
}

const MAX_NODES = 20_000
const MAX_DEPTH = 12

/**
 * Every scalar leaf in the probe result, addressed by a `simple` path.
 *
 * Capped rather than exhaustive: a hydration blob on a large retail page runs
 * to hundreds of thousands of nodes, and walking all of it freezes the tab for
 * a result nobody reads past the first screen.
 */
export function flattenProbe(probe: ProbeResult): PathHit[] {
  const hits: PathHit[] = []
  let budget = MAX_NODES

  function walk(node: unknown, kind: LocatorKind, path: string, depth: number) {
    if (budget <= 0 || depth > MAX_DEPTH) return
    if (node === null || node === undefined) return
    if (typeof node === 'object') {
      if (Array.isArray(node)) {
        for (let i = 0; i < node.length; i += 1) walk(node[i], kind, `${path}[${i}]`, depth + 1)
      } else {
        for (const [key, value] of Object.entries(node as Record<string, unknown>)) {
          walk(value, kind, path ? `${path}.${key}` : key, depth + 1)
        }
      }
      return
    }
    budget -= 1
    const text = String(node)
    if (text.trim()) hits.push({ kind, path, value: text })
  }

  walk(probe.json_ld, 'json_ld', '', 0)
  // Walked, not read as a string: `_set_meta_value` turns duplicate keys into
  // a *list* (several `og:locale:alternate` tags, say), so a meta value is not
  // always a scalar. Reading one flat would offer `og:locale:alternate` for
  // what is really `og:locale:alternate[0]`.
  for (const [key, value] of Object.entries(probe.metadata)) {
    walk(value, 'meta', key, 0)
  }
  for (const [container, value] of Object.entries(probe.hydration)) {
    walk(value, 'hydration', container, 0)
  }
  return hits
}

/**
 * Paths whose value contains `needle`.
 *
 * Shortest path first: `[0].name` and
 * `props.pageProps.initialData.data.product.name` may hold the same string,
 * and the shallow one is both easier to read and less likely to move.
 */
export function findPaths(hits: PathHit[], needle: string, limit = 40): PathHit[] {
  const trimmed = needle.trim().toLowerCase()
  if (trimmed.length < 2) return []
  return hits
    .filter((h) => h.value.toLowerCase().includes(trimmed))
    .sort((a, b) => a.path.length - b.path.length)
    .slice(0, limit)
}

export function hitToLocator(hit: PathHit, pathLang: PathLang = 'simple'): Locator {
  // `json_ld` paths are rooted at the array of blocks, which is how
  // `resolve_path` addresses them -- the leading `[0]` is not noise.
  return { kind: hit.kind, path: hit.path, path_lang: pathLang }
}

/**
 * `simple` path resolution, client-side.
 *
 * A faithful port of `recipe/v2/paths.py::_resolve_simple`, token rule
 * included: split on `.`/`[`/`]`, a list may only be indexed by an integer,
 * and anything that runs off the end is `null` rather than an error. A path
 * that resolves here must resolve identically at replay, so the two must agree
 * on all three of those.
 *
 * No recursive descent, matching the contract. `paths.py` explains why: on a
 * Walmart page an unanchored `$..name` matches 203 nodes, one of which is a
 * sponsored competitor's product name.
 */
export function resolveSimplePath(data: unknown, path: string): unknown {
  if (!path) return data
  let current: unknown = data
  for (const token of path.match(/[^.[\]]+/g) ?? []) {
    if (current === null || current === undefined) return null
    if (Array.isArray(current)) {
      if (!/^-?\d+$/.test(token)) return null
      const i = Number(token)
      current = i < 0 ? current[current.length + i] : current[i]
      if (current === undefined) current = null
    } else if (typeof current === 'object') {
      current = (current as Record<string, unknown>)[token] ?? null
    } else {
      return null
    }
  }
  return current
}

/**
 * Which probe container a structured locator kind reads from.
 *
 * Mirrors `evaluate.py::_SOURCE_TO_CONTAINER`, including the one place the
 * names differ: the locator kind is `meta`, the container is `metadata`.
 */
const KIND_TO_CONTAINER: Partial<Record<LocatorKind, keyof ProbeResult>> = {
  json_ld: 'json_ld',
  hydration: 'hydration',
  meta: 'metadata',
}

/** Resolve a structured locator against probed page data. `null` if it is not one. */
export function resolveStructuredLocator(probe: ProbeResult, locator: Locator): unknown {
  const container = KIND_TO_CONTAINER[locator.kind]
  if (!container) return null
  if (locator.path_lang === 'jmespath') return null // server-side only; see `paths.py`
  return resolveSimplePath(probe[container], locator.path ?? '')
}
