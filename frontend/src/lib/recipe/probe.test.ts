import { describe, it, expect } from 'vitest'
import {
  flattenProbe,
  parseProbe,
  resolveSimplePath,
  resolveStructuredLocator,
  type ProbeResult,
} from './probe'

/**
 * The studio proposes a JSON path; replay resolves it. If the two disagree
 * about either the shape of the page data or the meaning of a path, every
 * such field comes back empty at run time and nothing says why.
 *
 * These fix both halves of that agreement against what the server actually
 * does: `extraction/structured_data.py` for the shape, `recipe/v2/paths.py`
 * for the traversal.
 */

/** The payload shape `extract_structured_data` returns, as replay parses it. */
function probeOf(partial: Partial<ProbeResult>): ProbeResult {
  return { json_ld: [], metadata: {}, hydration: {}, ...partial }
}

describe('parseProbe', () => {
  it('reads the extractor payload the same way PageReader does', () => {
    const raw = JSON.stringify({
      json_ld: [{ name: 'Dress' }],
      metadata: { 'og:title': 'Dress' },
      hydration: { __NEXT_DATA__: { props: {} } },
    })
    const probe = parseProbe(raw)
    expect(probe.json_ld).toEqual([{ name: 'Dress' }])
    expect(probe.metadata['og:title']).toBe('Dress')
    expect(probe.hydration.__NEXT_DATA__).toEqual({ props: {} })
  })

  it('survives a page that yielded nothing, rather than throwing', () => {
    expect(parseProbe(undefined)).toEqual({ json_ld: [], metadata: {}, hydration: {} })
    expect(parseProbe('not json')).toEqual({ json_ld: [], metadata: {}, hydration: {} })
  })
})

describe('flattenProbe', () => {
  it('addresses json_ld entities as the server flattens them', () => {
    // `_flatten_json_ld` unwraps `@graph` and spreads a top-level array, so
    // the server's `json_ld` is a FLAT list of entities. The old in-page probe
    // pushed each <script> body whole, so it offered `[0].@graph[2].name` for
    // a value replay reads at `[2].name` -- and every path on a `@graph` page
    // (Yoast, Shopify, most CMSs) resolved to nothing.
    const probe = probeOf({ json_ld: [{ '@type': 'Product', name: 'Dress' }] })
    const hit = flattenProbe(probe).find((h) => h.value === 'Dress')
    expect(hit?.path).toBe('[0].name')
    expect(hit?.kind).toBe('json_ld')
    expect(resolveStructuredLocator(probe, { kind: 'json_ld', path: hit!.path })).toBe('Dress')
  })

  it('walks a meta value that the server turned into a list', () => {
    // `_set_meta_value` makes duplicate keys a list (several
    // `og:locale:alternate` tags). Reading one flat would offer the bare key
    // for what is really `og:locale:alternate[0]`.
    const probe = probeOf({ metadata: { 'og:locale:alternate': ['en_GB', 'fr_FR'] } })
    const hits = flattenProbe(probe).filter((h) => h.kind === 'meta')
    expect(hits.map((h) => h.path)).toEqual([
      'og:locale:alternate[0]',
      'og:locale:alternate[1]',
    ])
    expect(resolveStructuredLocator(probe, { kind: 'meta', path: hits[0].path })).toBe('en_GB')
  })

  it('keeps a scalar meta value addressable by its bare key', () => {
    const probe = probeOf({ metadata: { 'og:title': 'Dress' } })
    const hit = flattenProbe(probe).find((h) => h.kind === 'meta')
    expect(hit?.path).toBe('og:title')
    expect(resolveStructuredLocator(probe, { kind: 'meta', path: 'og:title' })).toBe('Dress')
  })

  it('roots hydration paths at the container key replay indexes by', () => {
    const probe = probeOf({
      hydration: { __NEXT_DATA__: { props: { pageProps: { name: 'Dress' } } } },
    })
    const hit = flattenProbe(probe).find((h) => h.value === 'Dress')
    expect(hit?.path).toBe('__NEXT_DATA__.props.pageProps.name')
    expect(resolveStructuredLocator(probe, { kind: 'hydration', path: hit!.path })).toBe('Dress')
  })
})

describe('resolveSimplePath', () => {
  // A port of `paths.py::_resolve_simple`. A path that resolves here must
  // resolve identically at replay, so the token rule has to match exactly.
  const data = {
    props: { pageProps: { modules: [{ name: 'a' }, { name: 'b' }] } },
    'og:title': 'kept whole',
  }

  it('walks dots and bracket indices', () => {
    expect(resolveSimplePath(data, 'props.pageProps.modules[1].name')).toBe('b')
  })

  it('treats a key with a colon as one token', () => {
    // Only `.`, `[` and `]` delimit -- so `og:title` is a single key, which is
    // what makes meta paths work at all.
    expect(resolveSimplePath(data, 'og:title')).toBe('kept whole')
  })

  it('supports a negative index, as the server does', () => {
    expect(resolveSimplePath(data, 'props.pageProps.modules[-1].name')).toBe('b')
  })

  it('returns null rather than throwing when a path runs off the end', () => {
    // "A path that compiles but matches nothing is NOT an error -- that is an
    // empty field, which is a normal outcome the candidate list handles."
    expect(resolveSimplePath(data, 'props.missing.deeper')).toBeNull()
    expect(resolveSimplePath(data, 'props.pageProps.modules[99].name')).toBeNull()
  })

  it('refuses to index a list by a non-integer', () => {
    expect(resolveSimplePath(data, 'props.pageProps.modules.name')).toBeNull()
  })

  it('returns the whole container for an empty path', () => {
    expect(resolveSimplePath(data, '')).toBe(data)
  })
})

describe('resolveStructuredLocator', () => {
  it('maps the meta kind onto the metadata container', () => {
    // The locator kind is `meta`; the container is `metadata`. That rename is
    // `_SOURCE_TO_CONTAINER`, and getting it wrong loses every meta field.
    const probe = probeOf({ metadata: { 'og:title': 'Dress' } })
    expect(resolveStructuredLocator(probe, { kind: 'meta', path: 'og:title' })).toBe('Dress')
  })

  it('declines a DOM locator instead of guessing', () => {
    const probe = probeOf({ metadata: { 'og:title': 'Dress' } })
    expect(resolveStructuredLocator(probe, { kind: 'css', selector: 'h1' })).toBeNull()
  })

  it('declines jmespath, which only the server can evaluate', () => {
    const probe = probeOf({ json_ld: [{ name: 'Dress' }] })
    expect(
      resolveStructuredLocator(probe, { kind: 'json_ld', path: '[0].name', path_lang: 'jmespath' }),
    ).toBeNull()
  })
})
