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

/** Runs in the page. Kept to one expression -- `execute_js` returns its value. */
export const PROBE_JS = `(() => {
  const out = { json_ld: [], meta: {}, hydration: {} };
  for (const s of document.querySelectorAll('script[type="application/ld+json"]')) {
    try { out.json_ld.push(JSON.parse(s.textContent)); } catch (e) { /* a malformed block is not a reason to lose the good ones */ }
  }
  for (const m of document.querySelectorAll('meta[property],meta[name]')) {
    const k = m.getAttribute('property') || m.getAttribute('name');
    if (k) out.meta[k] = m.getAttribute('content');
  }
  for (const id of ['__NEXT_DATA__', '__NUXT_DATA__']) {
    const el = document.getElementById(id);
    if (el) { try { out.hydration[id] = JSON.parse(el.textContent); } catch (e) {} }
  }
  for (const key of ['__NEXT_DATA__', '__NUXT__', '__INITIAL_STATE__', '__APOLLO_STATE__', '__PRELOADED_STATE__']) {
    if (window[key] !== undefined && out.hydration[key] === undefined) {
      try { out.hydration[key] = JSON.parse(JSON.stringify(window[key])); } catch (e) {}
    }
  }
  return out;
})()`

export interface ProbeResult {
  json_ld: unknown[]
  meta: Record<string, string | null>
  hydration: Record<string, unknown>
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
  for (const [key, value] of Object.entries(probe.meta)) {
    if (value?.trim()) hits.push({ kind: 'meta', path: key, value })
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
