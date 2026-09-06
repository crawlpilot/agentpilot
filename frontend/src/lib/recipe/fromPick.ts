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
import type { PreviewField } from '@/lib/picker/preview'
import { SOURCE_PRIORITY } from './document'
import type {
  Candidate,
  FieldGroup,
  FieldSpec,
  Locator,
  Recipe,
  Transform,
  TypeSpec,
  ValueType,
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
  /** Scope every candidate inside this locator -- the container, for a list. */
  within?: Locator
  /** Read this attribute rather than the element's text. */
  attribute?: string
  /** Return every match as a list rather than the first. */
  all?: boolean
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
  const { within, attribute, all, limit = 6 } = options

  // Persist only what will still resolve tomorrow. `filterPersistableChain`
  // also handles the degenerate case for us: on a page where *every* selector
  // is positional it keeps them all, since a volatile binding still beats a
  // field with no binding at all.
  const kept = filterPersistableChain(chain) ?? chain

  return kept.slice(0, limit).map((entry, index) => {
    const kind = isXPath(entry.selector) ? 'xpath' : 'css'
    const locator: Locator = { kind, selector: entry.selector }
    if (attribute) locator.attribute = attribute
    if (all) locator.all = true
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

/**
 * Scope a list read to the detected container.
 *
 * Without it, an item selector as general as `.card` collects the page's other
 * cards too -- the container is what makes a loose selector safe, which is why
 * the picker derives both. Note this is the *container*, never the row:
 * `within` resolves to `containers[0]` (`recipe/v2/evaluate.py`), so scoping to
 * a row selector would silently read the first row only.
 */
function containerScope(payload: PickPayload): Locator | undefined {
  if (!payload.containerSelector) return undefined
  return {
    kind: isXPath(payload.containerSelector) ? 'xpath' : 'css',
    selector: payload.containerSelector,
  }
}

/**
 * Compose a document-level selector for a column of a repeating list.
 *
 * The picker gives a row selector and a row-*relative* column selector; a
 * single `all: true` read needs them as one descendant expression, because
 * `within` cannot be the row (see `containerScope`). `li.card` + `.t` becomes
 * `li.card .t`, which matches one node per row, in row order.
 *
 * `:scope >` is a relative-selector form that only means anything inside a
 * `querySelectorAll` on the row itself; rewritten to a plain child combinator
 * it carries the same meaning in the composed expression.
 */
function composeColumnSelector(itemSelector: string, columnSelector: string): string | null {
  if (isXPath(itemSelector) || isXPath(columnSelector)) return null
  const relative = columnSelector.trim().replace(/^:scope\s*/, '')
  if (!relative) return null
  return relative.startsWith('>') ? `${itemSelector} ${relative}` : `${itemSelector} ${relative}`
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
 * A repeating list becomes one `list`-typed field per column.
 *
 * This is not the shape you might expect, and the reason is a real limit in the
 * replay engine rather than a preference. A `table` field's rows come only from
 * `RepeatSpec`, and `RepeatSpec` has exactly two kinds
 * (`recipe/v2/replay.py::_replay_repeat`): `json`, which iterates an array
 * already in the page's structured data, and `dom`, which *clicks through an
 * option set* and re-reads the page after each click. Neither describes "N
 * cards already rendered on a search page" -- and modelling that as a `dom`
 * repeat would click every card, navigating away from the page on the first
 * one.
 *
 * What the engine does execute today is a column-wise read: a locator with
 * `all: true` returns every match as a list, in document order. So each picked
 * column becomes `list<T>` bound to `"<row> <column>"`, and the caller gets
 * parallel arrays that zip into rows by index.
 *
 * The honest cost: nothing enforces that the arrays stay aligned. A card
 * missing a price yields a shorter price list and silently shifts every value
 * after it. A `dom_rows` repeat kind -- `rows_locator` over DOM nodes, columns
 * resolved per row, exactly `_rows_from_json` but against elements -- is the
 * fix, and it is a backend change deliberately outside this port's scope.
 */
export function listPickToDrafts(payload: PickPayload): {
  drafts: FieldDraft[]
  count: number
} {
  const columns = payload.data?.columns ?? []
  const firstRow = payload.data?.items?.[0]
  const within = containerScope(payload)
  const itemSelector = payload.itemSelector
  const names: string[] = []

  const drafts = columns.map((col) => {
    const name = toFieldName(col.name, names)
    names.push(name)
    const mapped = COLUMN_VALUE_TYPE[col.type] ?? COLUMN_VALUE_TYPE.text

    const spec: FieldSpec = {
      type: { kind: 'list', items: { kind: 'scalar', value_type: mapped.value_type } },
      description: '',
    }
    if (mapped.transform) spec.transform = mapped.transform

    // Compose each row-relative column selector against the row selector, so
    // one read collects that column across every row.
    const chain: PickSelector[] = []
    for (const entry of col.locators ?? []) {
      const composed = itemSelector ? composeColumnSelector(itemSelector, entry.selector) : null
      if (composed) chain.push({ selector: composed, strategy: entry.strategy })
    }

    return {
      name,
      spec,
      candidates: chainToCandidates(chain, { within, attribute: col.attribute, all: true }),
      preview: firstRow ? String(firstRow[col.id] ?? '') : undefined,
    }
  })

  return { drafts, count: payload.count ?? 0 }
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
  options: { groupId?: string } = {},
): Recipe {
  const groupId = options.groupId ?? recipe.field_groups[0]?.group_id ?? 'core'
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

  return next
}

/**
 * The attribute a field reads, as one answer rather than per-candidate.
 *
 * Every candidate for a field reads the same thing -- they are alternative
 * routes to one value, not different values -- so the editor treats it as a
 * property of the field. `null` means the candidates disagree, which only
 * happens after hand-editing and is worth showing rather than silently
 * normalising away.
 */
export function readAttribute(candidates: Candidate[]): string | null {
  if (candidates.length === 0) return 'text'
  const first = candidates[0].locator.attribute ?? 'text'
  return candidates.every((c) => (c.locator.attribute ?? 'text') === first) ? first : null
}

/** Set the attribute every candidate of a field reads. */
export function setReadAttribute(candidates: Candidate[], attribute: string): Candidate[] {
  return candidates.map((candidate) => ({
    ...candidate,
    locator: {
      ...candidate.locator,
      // `text` is the reader's default; storing it explicitly is noise in the
      // exported document, and the structured kinds have no attribute at all.
      attribute: attribute === 'text' ? undefined : attribute,
    },
  }))
}

/**
 * Field drafts as the preview evaluator wants them.
 *
 * Structured candidates (`json_ld`, `hydration`, `meta`) are dropped rather
 * than faked: the preview reads the DOM, and a JSON path resolves against
 * data the reader here does not hold. Silently skipping them keeps the
 * preview honest -- a field bound only to a JSON path reports `empty` here and
 * the UI says why, which is better than inventing a value for it.
 */
export function toPreviewFields(drafts: FieldDraft[]): PreviewField[] {
  return drafts.map((draft) => ({
    name: draft.name,
    required: draft.spec.required,
    candidates: draft.candidates
      .filter((c) => c.locator.kind === 'css' || c.locator.kind === 'xpath')
      .map((c) => ({
        kind: c.locator.kind as 'css' | 'xpath',
        selector: c.locator.selector ?? '',
        attribute: c.locator.attribute,
        all: c.locator.all,
        index: c.locator.index,
        within:
          c.locator.within && (c.locator.within.kind === 'css' || c.locator.within.kind === 'xpath')
            ? { kind: c.locator.within.kind as 'css' | 'xpath', selector: c.locator.within.selector ?? '' }
            : undefined,
      })),
  }))
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
