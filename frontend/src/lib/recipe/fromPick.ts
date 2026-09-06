/**
 * Turns a visual pick into v2 recipe structures.
 *
 * This is the join between the two halves of the port. The extension answers
 * "what selector reaches this element, and what fallbacks does it have?"; a v2
 * recipe asks for an *ordered `Candidate[]`* per field, resolved by priority
 * until one yields (contract §6). Those are the same idea, so the ranked chain
 * the picker emits becomes the candidate list almost directly -- the ranking
 * has already been done, by generators that know about hash classes, Tailwind
 * utilities and `:nth-child` volatility.
 *
 * Two rules govern everything here:
 *
 * 1. **Priority is the source's, not the chain's.** `SOURCE_PRIORITY` ranks
 *    `json_ld: 10` above `css: 60` for a reason -- a path into embedded JSON
 *    outlives a redesign that breaks every selector on the page. Chain order
 *    only breaks ties *within* a source. This is what lets `withJsonAlternatives`
 *    slot a structured-data candidate above a picked CSS one and have replay
 *    genuinely prefer it.
 *
 * 2. **Volatile selectors never persist.** `filterPersistableChain` drops
 *    hash classes and `:nth-child`/`:nth-of-type` entries, exactly as the
 *    extension does before saving a recipe. They are fine for the pick that
 *    just happened and worthless tomorrow.
 */
import { filterPersistableChain } from '@/lib/picker/vendor/shared/selectors/stability'
import type { PickColumn, PickPayload, PickSelector } from '@/lib/picker/protocol'
import { SOURCE_PRIORITY } from './document'
import type {
  Candidate,
  FieldGroup,
  FieldSpec,
  Locator,
  Recipe,
  Transform,
  TypeSpec,
} from './types'

/** An XPath, as the vendored generators write them. */
function isXPath(selector: string): boolean {
  const s = selector.trim()
  return s.startsWith('/') || s.startsWith('(') || s.startsWith('./') || s.startsWith('id(')
}

/**
 * The value type behind a column, before it is wrapped for a list read.
 *
 * `attribute` is separate from `value_type` on purpose: reading `href` is a
 * property of the *locator*, while `url` is a property of the value. A column
 * can be a URL read from text (a printed link) or an `href` read as a string.
 */
const COLUMN_VALUE_TYPE: Record<PickColumn['type'], { value_type: ValueType; transform?: Transform[] }> = {
  text: { value_type: 'string' },
  markdown: { value_type: 'text' },
  number: { value_type: 'number' },
  price: { value_type: 'price' },
  date: { value_type: 'date' },
  email: { value_type: 'string' },
  phone: { value_type: 'string' },
  // A relative `href`/`src` is the common case, and a bare "/p/123" is useless
  // to a caller -- resolve against the page URL on the way out.
  url: { value_type: 'url', transform: [{ op: 'url_resolve' }] },
  image: { value_type: 'url', transform: [{ op: 'url_resolve' }] },
  image_array: { value_type: 'url', transform: [{ op: 'url_resolve' }] },
}

/** What a detail pick's auto-classified kind means as a type + locator read. */
const EXTRACTION_TYPE: Record<
  NonNullable<PickPayload['extractionType']>,
  { type: TypeSpec; attribute?: string; transform?: Transform[] }
> = {
  text: { type: { kind: 'scalar', value_type: 'string' } },
  link: {
    type: { kind: 'scalar', value_type: 'url' },
    attribute: 'href',
    transform: [{ op: 'url_resolve' }],
  },
  image: {
    type: { kind: 'scalar', value_type: 'url' },
    attribute: 'src',
    transform: [{ op: 'url_resolve' }],
  },
  text_array: { type: { kind: 'list', items: { kind: 'scalar', value_type: 'string' } } },
  link_array: {
    type: { kind: 'list', items: { kind: 'scalar', value_type: 'url' } },
    attribute: 'href',
    transform: [{ op: 'url_resolve' }],
  },
  image_array: {
    type: { kind: 'list', items: { kind: 'scalar', value_type: 'url' } },
    attribute: 'src',
    transform: [{ op: 'url_resolve' }],
  },
  list: { type: { kind: 'list', items: { kind: 'scalar', value_type: 'string' } } },
  table: { type: { kind: 'table', columns: {} } },
}

interface ChainOptions {
  /** Scope every candidate inside this locator -- a row, for list bindings. */
  within?: Locator
  /** Read this attribute rather than the element's text. */
  attribute?: string
  /** Cap the chain. A 15-deep fallback list is noise in the editor. */
  limit?: number
}

/**
 * A ranked selector chain becomes an ordered candidate list.
 *
 * `verified_on: 1` is deliberate and load-bearing. These selectors were not
 * guessed -- they were derived from an element the author clicked and, in list
 * mode, validated against every sibling by `CommonSelectorGenerator`'s majority
 * rule. Leaving it 0 would make `lintRecipe` flag every picked candidate as
 * "nobody has ever seen it work", which is precisely the warning that should
 * stay meaningful for the ones nobody has.
 */
export function chainToCandidates(
  chain: PickSelector[] | undefined,
  options: ChainOptions = {},
): Candidate[] {
  if (!chain?.length) return []
  const { within, attribute, limit = 6 } = options

  // Persist only what will still resolve tomorrow. `filterPersistableChain`
  // also handles the degenerate case for us: on a page where *every* selector
  // is positional it keeps them all, since a volatile binding still beats a
  // field with no binding at all.
  const kept = filterPersistableChain(chain) ?? chain

  return kept.slice(0, limit).map((entry, index) => {
    const kind = isXPath(entry.selector) ? 'xpath' : 'css'
    const locator: Locator = { kind, selector: entry.selector }
    if (attribute) locator.attribute = attribute
    if (within) locator.within = within
    return {
      // Source first, chain position second -- see the header note on
      // priority. The `+ index` only orders candidates of the same kind.
      priority: SOURCE_PRIORITY[kind] + index,
      locator,
      verified_on: 1,
      confidence: null,
      note: entry.strategy,
    }
  })
}

/** The best locator for the rows of a repeating group. */
function rowsLocator(payload: PickPayload): Locator | null {
  if (!payload.itemSelector) return null
  const locator: Locator = {
    kind: isXPath(payload.itemSelector) ? 'xpath' : 'css',
    selector: payload.itemSelector,
  }
  // Scope rows to the detected container. Without it, an item selector as
  // general as `.card` collects the page's other cards too -- the container is
  // what makes a loose selector safe, which is why the picker derives both.
  if (payload.containerSelector) {
    locator.within = {
      kind: isXPath(payload.containerSelector) ? 'xpath' : 'css',
      selector: payload.containerSelector,
    }
  }
  return locator
}

/** A column name the recipe document can key on. */
export function toFieldName(raw: string, taken: Iterable<string> = []): string {
  const used = new Set(taken)
  const base =
    raw
      .trim()
      .replace(/[^\w\s-]/g, '')
      .replace(/[\s-]+/g, '_')
      .replace(/^_+|_+$/g, '')
      .toLowerCase() || 'field'
  if (!used.has(base)) return base
  let n = 2
  while (used.has(`${base}_${n}`)) n += 1
  return `${base}_${n}`
}

export interface FieldDraft {
  name: string
  spec: FieldSpec
  candidates: Candidate[]
  /** What the picker read here, shown in the editor so a name can be chosen. */
  preview?: string
}

/**
 * A list pick becomes a set of field drafts plus the `repeat` that walks rows.
 *
 * Every column binds *relative to its row* (`within: rowsLocator`), which is
 * what makes one binding serve all N rows rather than only the first.
 */
export function listPickToDrafts(payload: PickPayload): {
  drafts: FieldDraft[]
  rows: Locator | null
  count: number
} {
  const rows = rowsLocator(payload)
  const columns = payload.data?.columns ?? []
  const firstRow = payload.data?.items?.[0]
  const names: string[] = []

  const drafts = columns.map((col) => {
    const name = toFieldName(col.name, names)
    names.push(name)
    const mapped = COLUMN_TYPE[col.type] ?? COLUMN_TYPE.text

    const spec: FieldSpec = { type: mapped.type, description: '' }
    if (mapped.transform) spec.transform = mapped.transform

    const chain: PickSelector[] = col.locators ?? []
    // The row-relative XPath is a last resort behind every CSS candidate, but
    // it is better than a field with no binding on a page that defeats CSS.
    if (col.xpath && !chain.some((c) => c.selector === col.xpath)) {
      chain.push({ selector: col.xpath, strategy: 'XPath' })
    }

    return {
      name,
      spec,
      candidates: chainToCandidates(chain, {
        within: rows ?? undefined,
        attribute: col.attribute,
      }),
      preview: firstRow ? String(firstRow[col.id] ?? '') : undefined,
    }
  })

  return { drafts, rows, count: payload.count ?? 0 }
}

/** A detail pick becomes one field draft. */
export function detailPickToDraft(payload: PickPayload, taken: Iterable<string> = []): FieldDraft {
  const mapped = EXTRACTION_TYPE[payload.extractionType ?? 'text'] ?? EXTRACTION_TYPE.text
  const spec: FieldSpec = { type: mapped.type, description: '' }
  if (mapped.transform) spec.transform = mapped.transform

  const chain: PickSelector[] =
    payload.itemSelectors?.length
      ? payload.itemSelectors
      : payload.containerSelector
        ? [{ selector: payload.containerSelector, strategy: 'Picked' }]
        : []
  if (payload.containerXPath && !chain.some((c) => c.selector === payload.containerXPath)) {
    chain.push({ selector: payload.containerXPath, strategy: 'XPath' })
  }

  return {
    name: toFieldName(payload.tagName ?? 'field', taken),
    spec,
    candidates: chainToCandidates(chain, { attribute: mapped.attribute }),
    preview: payload.previewValue,
  }
}

/**
 * Write drafts into a recipe as a new group, with its `repeat` when the pick
 * was a repeating list.
 *
 * Goes through the document's own helpers rather than assembling the object
 * directly: a field name is a key in `fields`, in `field_groups[].field_names`
 * *and* in `field_groups[].bindings`, and `document.ts` exists to keep those
 * three from drifting.
 */
export function applyDrafts(
  recipe: Recipe,
  drafts: FieldDraft[],
  options: { groupId: string; rows?: Locator | null; maxRows?: number } = { groupId: 'core' },
): Recipe {
  const { groupId, rows, maxRows } = options
  let next = recipe

  if (!next.field_groups.some((g) => g.group_id === groupId)) {
    const group: FieldGroup = { group_id: groupId, field_names: [], bindings: {}, steps: [] }
    next = { ...next, field_groups: [...next.field_groups, group] }
  }

  for (const draft of drafts) {
    if (next.fields[draft.name]) continue
    next = {
      ...next,
      fields: { ...next.fields, [draft.name]: draft.spec },
      field_groups: next.field_groups.map((g) =>
        g.group_id === groupId
          ? {
              ...g,
              field_names: [...g.field_names, draft.name],
              bindings: { ...g.bindings, [draft.name]: draft.candidates },
            }
          : g,
      ),
    }
  }

  if (rows) {
    const rowField = drafts[0]?.name
    if (rowField) {
      next = {
        ...next,
        field_groups: next.field_groups.map((g) =>
          g.group_id === groupId
            ? {
                ...g,
                repeat: {
                  kind: 'dom',
                  rows_locator: rows,
                  row_field: rowField,
                  max_iterations: maxRows ?? 100,
                },
              }
            : g,
        ),
      }
    }
  }

  return next
}

/**
 * Offer a structured-data path *above* a picked CSS candidate when the value
 * is also embedded in the page's JSON.
 *
 * `docs/recipe-studio.md` calls this the single highest-value nudge the studio
 * can give, and it is the reason porting a CSS picker does not make recipes
 * more brittle. On a page carrying JSON-LD or hydration state the same value
 * usually has a path that survives a redesign; the author clicked the rendered
 * text because that is what they could see, not because CSS was the right
 * answer. Ranking by `SOURCE_PRIORITY` means the JSON path is what replay
 * actually tries first, with the picked selector kept as the fallback.
 */
export function withJsonAlternatives(candidates: Candidate[], alternatives: Locator[]): Candidate[] {
  if (alternatives.length === 0) return candidates
  const structured = alternatives.map((locator, index) => ({
    priority: SOURCE_PRIORITY[locator.kind] + index,
    locator,
    verified_on: 1,
    confidence: null,
    note: 'same value found in page JSON',
  }))
  return [...structured, ...candidates].sort((a, b) => (a.priority ?? 0) - (b.priority ?? 0))
}
