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
import type { HighlightField, PickColumn, PickPayload, PickSelector } from '@/lib/picker/protocol'
import type { PreviewField, PreviewLocator, PreviewRowsField } from '@/lib/picker/preview'
import { SOURCE_PRIORITY } from './document'
import type {
  Candidate,
  FieldGroup,
  Step,
  FieldSpec,
  Locator,
  Recipe,
  RepeatSpec,
  Transform,
  TypeSpec,
  ValueType,
} from './types'

/** An XPath, as the vendored generators write them. */
export function isXPath(selector: string): boolean {
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
  /** Scalar and list fields. Empty for a table, which binds by column. */
  candidates: Candidate[]
  /** What the picker read here, shown in the editor so a name can be chosen. */
  preview?: string

  // --- table fields only ---
  /**
   * Column name -> its ordered candidates, each resolved *relative to a row*.
   * A table's bindings are keyed by column, not by the field name (contract
   * S8, and the shape the Zara and Walmart examples use).
   */
  columns?: Record<string, Candidate[]>
  /** How the rows are produced. `dom_rows` for a picked list. */
  repeat?: RepeatSpec
  /** First-row values per column, so the author can recognise what to name. */
  columnPreviews?: Record<string, string>
}

/**
 * A repeating list becomes ONE `table` field, with a `dom_rows` repeat.
 *
 * This is what contract v2.1's `dom_rows` kind exists for, and it replaces a
 * genuinely bad workaround. Until it existed, `RepeatSpec` had only `json`
 * (the data must already be an array) and `dom` (which *clicks*, and on a
 * results page navigates away on the first row) -- so a picked list had to be
 * emitted as one `all: true` list field per column, with the caller zipping by
 * index. That silently shifted every value below any row that happened to lack
 * a cell, and nothing in the output said it had happened.
 *
 * Row-wise, the columns keep their *row-relative* selectors exactly as the
 * picker derived them: no composing `li.card` onto `.title`, no `all: true`,
 * no container `within` on each column. The row is the scope, supplied by the
 * iteration. Simpler to produce, and correct by construction.
 */
export function listPickToDrafts(payload: PickPayload): {
  drafts: FieldDraft[]
  count: number
} {
  const columns = payload.data?.columns ?? []
  const firstRow = payload.data?.items?.[0]
  const count = payload.count ?? 0

  if (!payload.itemSelector || columns.length === 0) return { drafts: [], count }

  const rowsLocator: Locator = {
    kind: isXPath(payload.itemSelector) ? 'xpath' : 'css',
    selector: payload.itemSelector,
  }
  // The container is what makes a loose item selector safe -- `.card` alone
  // would collect the page's other cards too.
  const within = containerScope(payload)
  if (within) rowsLocator.within = within

  const bindings: Record<string, Candidate[]> = {}
  const typeColumns: Record<string, TypeSpec> = {}
  const columnPreviews: Record<string, string> = {}
  const names: string[] = []

  for (const col of columns) {
    const name = toFieldName(col.name, names)
    names.push(name)
    const mapped = COLUMN_VALUE_TYPE[col.type] ?? COLUMN_VALUE_TYPE.text

    const chain: PickSelector[] = [...(col.locators ?? [])]
    if (col.xpath && !chain.some((c) => c.selector === col.xpath)) {
      chain.push({ selector: col.xpath, strategy: 'XPath' })
    }
    const candidates = chainToCandidates(chain, { attribute: col.attribute })
    if (candidates.length === 0) continue

    bindings[name] = candidates
    typeColumns[name] = { kind: 'scalar', value_type: mapped.value_type }
    if (firstRow) columnPreviews[name] = String(firstRow[col.id] ?? '')
  }

  if (Object.keys(bindings).length === 0) return { drafts: [], count }

  const draft: FieldDraft = {
    name: 'items',
    spec: { type: { kind: 'table', columns: typeColumns }, description: '' },
    candidates: [],
    columns: bindings,
    columnPreviews,
    repeat: {
      kind: 'dom_rows',
      rows_locator: rowsLocator,
      row_field: 'items',
      // Generous but bounded: a results page is tens of rows, and a runaway
      // selector matching thousands should be reported as truncated rather
      // than read in full.
      max_iterations: Math.max(count * 2, 100),
    },
  }

  return { drafts: [draft], count }
}

/**
 * A path into the page's embedded JSON becomes a field draft.
 *
 * `SOURCE_PRIORITY` ranks these far above CSS (`json_ld: 10` vs `css: 60`),
 * and deservedly: a path into JSON-LD or hydration state survives a redesign
 * that breaks every selector on the page.
 *
 * `verified_on: 1` because the path was read *out of this page* a moment ago
 * -- it is not a guess, and the lint's "never verified" warning has to keep
 * meaning something for the candidates that are.
 */
export function jsonHitToDraft(
  hit: { kind: 'json_ld' | 'hydration' | 'meta'; path: string; value: string },
  taken: Iterable<string> = [],
): FieldDraft {
  const leaf = hit.path.split(/[.[\]]/).filter(Boolean).pop() ?? 'field'
  return {
    name: toFieldName(leaf, taken),
    spec: { type: { kind: 'scalar', value_type: 'string' }, description: '' },
    candidates: [
      {
        priority: SOURCE_PRIORITY[hit.kind],
        locator: { kind: hit.kind, path: hit.path, path_lang: 'simple' },
        verified_on: 1,
        confidence: null,
        note: `read from ${hit.kind}`,
      },
    ],
    preview: hit.value,
  }
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
 * One entry in the authoring list: a field to read, an action to perform, or a
 * marker that the page must be reloaded first.
 *
 * Fields and actions live in *one ordered list* because on a real page they
 * interleave -- a value behind a drawer needs the click that opens it, and the
 * click is only meaningful before that particular read. Keeping them in
 * separate steps forces an author to jump back and forth to express one
 * sequence, which is the arrangement the extension avoids and the one this
 * replaced.
 */
export type WorkItem =
  | { kind: 'field'; id: string; draft: FieldDraft }
  | { kind: 'action'; id: string; step: Step }
  | { kind: 'reset'; id: string }

/**
 * Compile the authoring list into `global_setup` + `field_groups`.
 *
 * The mapping is dictated by one fact about replay: `_replay_group`
 * **re-navigates at the start of every group** and re-runs `global_setup` each
 * time (`recipe/v2/replay.py`). A group therefore does not inherit the page
 * state an earlier group left behind -- it starts from a fresh load, every
 * time. Three consequences, and all three are why this function is not a
 * one-liner:
 *
 * 1. **Leading actions become `global_setup`.** Actions before the first field
 *    are the ones every group needs -- the cookie banner, the region prompt --
 *    and `global_setup` is re-run per group, which is exactly right for them.
 *
 * 2. **A group's steps are cumulative, not incremental.** For `[A1, F1, A2,
 *    F2, A3, F3]`, the group holding `F3` needs `[A2, A3]`, not `[A3]`: it
 *    re-navigated, so `A2`'s effect is gone. Emitting only the incremental
 *    action is the subtle bug this exists to avoid, and it would fail only on
 *    progressively-revealed content -- late, and in production.
 *
 * 3. **`reset` exists because cumulative is not always right.** Two drawers
 *    that close each other cannot both be open, so accumulating their opens
 *    produces a sequence that cannot run. The Zara example hits this exactly
 *    (`_readme`: "two drawers that conflict so they need separate groups"). A
 *    `reset` clears the accumulation and starts a genuinely independent group.
 */
export function itemsToRecipe(recipe: Recipe, items: WorkItem[]): Recipe {
  const firstFieldAt = items.findIndex((i) => i.kind === 'field')
  const firstResetAt = items.findIndex((i) => i.kind === 'reset')

  // Leading actions: everything before the first field, and before any reset
  // (a reset before any field would make them not-global after all).
  const leadingEnd =
    firstFieldAt === -1
      ? items.length
      : firstResetAt !== -1 && firstResetAt < firstFieldAt
        ? firstResetAt
        : firstFieldAt

  const globalSetup = items.slice(0, leadingEnd).filter((i) => i.kind === 'action').map((i) => i.step)

  interface Pending {
    steps: Step[]
    drafts: FieldDraft[]
  }
  const groups: Pending[] = []
  let accumulated: Step[] = []
  let current: Pending | null = null

  for (const item of items.slice(leadingEnd)) {
    if (item.kind === 'reset') {
      accumulated = []
      current = null
      continue
    }
    if (item.kind === 'action') {
      accumulated = [...accumulated, item.step]
      // The next field belongs to a new group: it needs this action, and the
      // fields already placed did not.
      current = null
      continue
    }
    if (current === null) {
      current = { steps: accumulated, drafts: [] }
      groups.push(current)
    }
    current.drafts.push(item.draft)
  }

  let next: Recipe = {
    ...recipe,
    fields: {},
    field_groups: [],
    global_setup: globalSetup,
  }

  // Nothing picked yet: keep a single empty group so the document stays the
  // shape `emptyRecipe` promises rather than becoming group-less.
  if (groups.length === 0) {
    return { ...next, field_groups: [{ group_id: 'core', field_names: [], bindings: {}, steps: [] }] }
  }

  groups.forEach((group, index) => {
    const groupId = groups.length === 1 ? 'core' : `group_${index + 1}`
    const bindings: Record<string, Candidate[]> = {}
    const fieldNames: string[] = []
    let repeat: RepeatSpec | undefined
    for (const draft of group.drafts) {
      if (next.fields[draft.name]) continue
      next = { ...next, fields: { ...next.fields, [draft.name]: draft.spec } }
      fieldNames.push(draft.name)
      if (draft.columns) {
        // A table binds by COLUMN, not by the field name -- the field itself
        // appears only in `field_names`. See `FieldDraft.columns`.
        Object.assign(bindings, draft.columns)
        if (draft.repeat) repeat = draft.repeat
      } else {
        bindings[draft.name] = draft.candidates
      }
    }
    const fieldGroup: FieldGroup = { group_id: groupId, field_names: fieldNames, bindings }
    if (group.steps.length > 0) fieldGroup.steps = group.steps
    if (repeat) fieldGroup.repeat = repeat
    next = { ...next, field_groups: [...next.field_groups, fieldGroup] }
  })

  return next
}

/** A picked selector string as a `Locator`, with its kind detected. */
export function toLocator(selector: string): Locator {
  return { kind: isXPath(selector) ? 'xpath' : 'css', selector }
}

/**
 * Field drafts as on-page markers, so the page shows what has been taken.
 *
 * Only DOM candidates can be marked -- a `json_ld` path has no element to draw
 * a box around -- and only the *winning* one, since the marker should show
 * where the value will actually come from rather than every place it might.
 */
export function toHighlightFields(drafts: FieldDraft[]): HighlightField[] {
  return drafts.flatMap((draft, index) => {
    // A table binds by column and carries no top-level candidates, so reading
    // `draft.candidates` here returned nothing and a picked *list* -- the
    // commonest pick there is -- left the page completely unmarked.
    if (draft.columns) {
      const rows = draft.repeat?.rows_locator
      if (!rows?.selector || rows.kind === 'xpath') return []
      return Object.entries(draft.columns).flatMap(([column, candidates]) => {
        const dom = candidates.find(
          (c) => c.locator.kind === 'css' && c.locator.selector,
        )
        if (!dom) return []
        // Composed to document scope: the marker has to be findable from the
        // page, and the column selectors are row-relative. This marks the
        // first row's cells, which is what tells an author their column is
        // pointing where they think it is.
        return [
          {
            id: `${index}:${draft.name}:${column}`,
            name: column,
            action: 'extract' as const,
            selectors: [{ type: 'css', value: `${rows.selector} ${dom.locator.selector}` }],
          },
        ]
      })
    }

    const dom = draft.candidates.find(
      (c) => (c.locator.kind === 'css' || c.locator.kind === 'xpath') && c.locator.selector,
    )
    if (!dom) return []
    return [
      {
        id: `${index}:${draft.name}`,
        name: draft.name,
        action: 'extract' as const,
        selectors: [{ type: dom.locator.kind === 'xpath' ? 'xpath' : 'css', value: dom.locator.selector! }],
      },
    ]
  })
}

/** Reveal steps as on-page markers, in the amber "this gets clicked" colour. */
export function stepsToHighlightFields(steps: { op: string; target?: Locator }[]): HighlightField[] {
  return steps.flatMap((step, index) => {
    const selector = step.target?.selector
    if (!selector) return []
    return [
      {
        id: `step:${index}`,
        name: `${index + 1}. ${step.op}`,
        action: 'click' as const,
        selectors: [{ type: step.target?.kind === 'xpath' ? 'xpath' : 'css', value: selector }],
      },
    ]
  })
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
 * Scalar field drafts as the preview evaluator wants them.
 *
 * Structured candidates (`json_ld`, `hydration`, `meta`) are dropped rather
 * than faked: the preview reads the DOM, and a JSON path resolves against data
 * the reader here does not hold. A field bound only to a JSON path reports
 * `empty` and the UI says why, which beats inventing a value for it.
 */
export function toPreviewFields(drafts: FieldDraft[]): PreviewField[] {
  // Table drafts read row-wise instead -- see `toPreviewRowsFields`.
  return drafts
    .filter((d) => !d.columns)
    .map((draft) => ({
      name: draft.name,
      required: draft.spec.required,
      candidates: toPreviewLocators(draft.candidates),
    }))
}

function toPreviewLocators(candidates: Candidate[]): PreviewLocator[] {
  return candidates
    .filter((c) => c.locator.kind === 'css' || c.locator.kind === 'xpath')
    .map((c) => ({
      kind: c.locator.kind as 'css' | 'xpath',
      selector: c.locator.selector ?? '',
      attribute: c.locator.attribute,
      all: c.locator.all,
      index: c.locator.index,
    }))
}

/** Table drafts as row-wise preview requests. Scalar drafts are ignored. */
export function toPreviewRowsFields(drafts: FieldDraft[]): PreviewRowsField[] {
  return drafts.flatMap((draft) => {
    const rows = draft.repeat?.rows_locator
    if (!draft.columns || !rows?.selector) return []
    if (rows.kind !== 'css' && rows.kind !== 'xpath') return []
    return [
      {
        name: draft.name,
        rows: {
          kind: rows.kind,
          selector: rows.selector,
          within:
            rows.within && (rows.within.kind === 'css' || rows.within.kind === 'xpath')
              ? { kind: rows.within.kind, selector: rows.within.selector ?? '' }
              : undefined,
        },
        columns: Object.fromEntries(
          Object.entries(draft.columns).map(([name, cands]) => [name, toPreviewLocators(cands)]),
        ),
        maxRows: draft.repeat?.max_iterations ?? 100,
      },
    ]
  })
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
