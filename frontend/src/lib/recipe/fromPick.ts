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
import { hitToLocator, type PathHit } from './probe'
import type {
  Candidate,
  FieldGroup,
  Step,
  FieldSpec,
  Locator,
  Recipe,
  RepeatSpec,
  StepOp,
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

/**
 * What a detail pick's auto-classified kind means as a type + locator read.
 *
 * Only the *scalar and array* kinds are here. `list` and `table` are not a
 * value at one selector -- they are a repeating set, and they go through
 * `containerPickToDraft` instead. Mapping them here was the bug: `table`
 * produced `{kind: 'table', columns: {}}`, a table with no columns bound to
 * the container element, which replay reads as one scalar and returns as a
 * single blob of every cell's text run together.
 *
 * The array kinds carry a `member` selector because the picked element is the
 * *wrapper* -- clicking a gallery selects the gallery, and the values are its
 * children. `ElementProcessor::performExtractAction` reaches them with exactly
 * these queries; a locator that reads the wrapper itself with `all: true`
 * matches one node and yields a one-item list containing every value
 * concatenated, which is the same failure in a different shape.
 */
const EXTRACTION_TYPE: Record<
  'text' | 'link' | 'image' | 'text_array' | 'link_array' | 'image_array',
  {
    type: TypeSpec
    attribute?: string
    transform?: Transform[]
    /** Read these inside the picked wrapper. Both kinds -- see `memberCandidates`. */
    member?: { css: string; xpath: string }
  }
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
  text_array: {
    type: { kind: 'list', items: { kind: 'scalar', value_type: 'string' } },
    // The direct-children read, matching the extension's `element.children`
    // walk. `filter_empty` drops the separators and empty wrappers that walk
    // skips by tag and this one cannot.
    member: { css: ':scope > *', xpath: './*' },
    transform: [{ op: 'filter_empty' }],
  },
  link_array: {
    type: { kind: 'list', items: { kind: 'scalar', value_type: 'url' } },
    attribute: 'href',
    member: { css: 'a[href]', xpath: './/a[@href]' },
    transform: [{ op: 'url_resolve' }],
  },
  image_array: {
    type: { kind: 'list', items: { kind: 'scalar', value_type: 'url' } },
    attribute: 'src',
    member: { css: 'img', xpath: './/img' },
    transform: [{ op: 'url_resolve' }],
  },
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
  /**
   * The rows collapse to one `{label: value}` map rather than staying a list
   * of row objects. See `setKeyValue`, which is what actually expresses it in
   * the document.
   */
  keyValue?: boolean
}

/** The two columns a key-value table reads: the label, and what it labels. */
export const KEY_COLUMN = 'name'
export const VALUE_COLUMN = 'value'

/**
 * Read a 2-column table as a `{label: value}` map, or back as rows.
 *
 * A specification table -- `Brand | Nike`, `Colour | Red` -- is a table on the
 * page and a *map* in the answer. Nobody consuming a product feed wants
 * `[{name: "Brand", value: "Nike"}, ...]`; they want `{"Brand": "Nike"}`, and
 * turning one into the other downstream means every caller writes the same
 * loop.
 *
 * `to_object` is the contract's own name for this shape (`transform.py`:
 * "`[{name, value}, ...] -> {name: value}` -- the near-universal specification
 * shape"). The type stays `table`: the *read* really is two columns of rows,
 * the map is what happens to them afterwards, and keeping it a table is also
 * what lets `validate_document` check both columns are bound.
 *
 * Turning it off leaves the columns named `name`/`value`, which is what they
 * are regardless of whether the map is wanted.
 */
export function setKeyValue(draft: FieldDraft, on: boolean): FieldDraft {
  if (!draft.columns) return draft
  const names = Object.keys(draft.columns)
  if (names.length !== 2) return draft

  const rename = <T,>(record: Record<string, T> | undefined): Record<string, T> | undefined => {
    if (!record) return record
    const [first, second] = names
    return Object.fromEntries(
      Object.entries(record).map(([key, value]) => [
        key === first ? KEY_COLUMN : key === second ? VALUE_COLUMN : key,
        value,
      ]),
    )
  }

  const transform = (draft.spec.transform ?? []).filter((t) => t.op !== 'to_object')
  const spec: FieldSpec = {
    ...draft.spec,
    type: { ...draft.spec.type, columns: rename(draft.spec.type.columns) },
    transform: on ? [...transform, { op: 'to_object' }] : transform,
  }
  if (spec.transform?.length === 0) delete spec.transform

  return {
    ...draft,
    keyValue: on,
    spec,
    columns: rename(draft.columns),
    columnPreviews: rename(draft.columnPreviews),
  }
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
  const draft = containerPickToDraft(payload, 'items')
  return { drafts: draft ? [draft] : [], count: payload.count ?? 0 }
}

/**
 * One repeating set -> one `table` field with a `dom_rows` repeat.
 *
 * Shared by both doors onto the same thing. A *list-mode* pick arrives here
 * because that is what list mode is for; a *detail-mode* pick arrives here
 * when the author clicked something that turned out to be a table or a list,
 * which `DetailSelectionStrategy` detects and classifies but -- until
 * `enrich.ts` fills in a row selector -- could not express. Both carry the
 * same `data.columns` from the same `DataExtractor`, so both build the same
 * field, and the only difference is what it gets called.
 */
export function containerPickToDraft(
  payload: PickPayload,
  name: string,
  taken: Iterable<string> = [],
): FieldDraft | null {
  const columns = payload.data?.columns ?? []
  const firstRow = payload.data?.items?.[0]
  const count = payload.count ?? 0

  if (!payload.itemSelector || columns.length === 0) return null

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
    const columnName = toFieldName(col.name, names)
    names.push(columnName)
    const mapped = COLUMN_VALUE_TYPE[col.type] ?? COLUMN_VALUE_TYPE.text

    const chain: PickSelector[] = [...(col.locators ?? [])]
    if (col.xpath && !chain.some((c) => c.selector === col.xpath)) {
      chain.push({ selector: col.xpath, strategy: 'XPath' })
    }
    const candidates = chainToCandidates(chain, { attribute: col.attribute })
    if (candidates.length === 0) continue

    bindings[columnName] = candidates
    typeColumns[columnName] = { kind: 'scalar', value_type: mapped.value_type }
    if (firstRow) columnPreviews[columnName] = String(firstRow[col.id] ?? '')
  }

  if (Object.keys(bindings).length === 0) return null

  const fieldName = toFieldName(name, taken)
  const draft: FieldDraft = {
    name: fieldName,
    spec: { type: { kind: 'table', columns: typeColumns }, description: '' },
    candidates: [],
    columns: bindings,
    columnPreviews,
    repeat: {
      kind: 'dom_rows',
      rows_locator: rowsLocator,
      row_field: fieldName,
      // Generous but bounded: a results page is tens of rows, and a runaway
      // selector matching thousands should be reported as truncated rather
      // than read in full.
      max_iterations: Math.max(count * 2, 100),
    },
  }

  // `keyValue` is set by `enrich.ts` only where the markup states it outright.
  // Every other spec table is a toggle in the wizard, not a guess here.
  return payload.keyValue ? setKeyValue(draft, true) : draft
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
export function jsonHitToDraft(hit: PathHit, taken: Iterable<string> = []): FieldDraft {
  // The leaf of the path is the best name available -- `offers.price` wants to
  // be called `price`, not `offers_price`. The author renames it if not.
  const leaf = hit.path.split(/[.[\]]/).filter(Boolean).pop() ?? 'field'
  return {
    name: toFieldName(leaf, taken),
    spec: { type: { kind: 'scalar', value_type: 'string' }, description: '' },
    candidates: [
      {
        priority: SOURCE_PRIORITY[hit.kind],
        // `hitToLocator` is the studio's existing probe -> locator conversion;
        // duplicating it here is how the two would come to disagree.
        locator: hitToLocator(hit),
        verified_on: 1,
        confidence: null,
        note: `read from ${hit.kind}`,
      },
    ],
    preview: hit.value,
  }
}

/**
 * A detail pick becomes one field draft.
 *
 * Three shapes come through here, and conflating them is what made a picked
 * table return one long string:
 *
 * 1. **A repeating set** (`list` / `table`). The author clicked a table or a
 *    listing; `DetailSelectionStrategy` detected it and extracted every row.
 *    That is a `dom_rows` table field, identical to a list-mode pick, so it is
 *    built by the same function.
 * 2. **An array** (`text_array` / `link_array` / `image_array`). The clicked
 *    element is the wrapper; the values are its children, read with `all` and
 *    a member selector scoped inside it.
 * 3. **One value** (`text` / `link` / `image`). The original case: a chain of
 *    candidates for the element itself.
 */
export function detailPickToDraft(payload: PickPayload, taken: Iterable<string> = []): FieldDraft {
  const kind = payload.extractionType ?? 'text'

  // Gated on the classification, not on the presence of `data`: `DataExtractor`
  // fakes `type: 'list'` with pseudo-columns for a *single* element too (see
  // `extractSingle` -- "[CRITICAL] Fake it as a 'list'"), so a plain text pick
  // also arrives carrying columns. Only the classifier knows the difference.
  if (kind === 'list' || kind === 'table') {
    const container = containerPickToDraft(payload, payload.keyValue ? 'specs' : 'items', taken)
    if (container) return container
  }

  // `list`/`table` reach here only when the pick could not be turned into a
  // repeating read -- no row selector, or no column resolved. Falling back to
  // the wrapper's own text is what the extension does in the same spot
  // (`ElementProcessor`: `value = element.innerText`), and it is at least a
  // value the author can see is wrong, rather than an empty field.
  const mapped =
    kind in EXTRACTION_TYPE
      ? EXTRACTION_TYPE[kind as keyof typeof EXTRACTION_TYPE]
      : EXTRACTION_TYPE.text
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
    candidates: mapped.member
      ? memberCandidates(chain, mapped.member, mapped.attribute)
      : chainToCandidates(chain, { attribute: mapped.attribute }),
    preview: payload.previewValue,
  }
}

/**
 * An array field's candidates: read `member` inside each candidate wrapper.
 *
 * The wrapper chain is the fallback chain -- `within` is what carries it, so a
 * wrapper selector that stops resolving falls through to the next exactly as
 * it would for a scalar. The values' relationship to their wrapper is the one
 * thing here that does not vary, so only the scope changes down the chain.
 *
 * **The member takes its wrapper's kind, and must.** A `css` locator scoped by
 * an `xpath` one is rejected by the lint and by `validate_document` -- the two
 * are composed rather than resolved separately -- so a chain that ends in
 * XPath candidates (most do) would have made the field unsaveable. The xpath
 * forms are written `./` and `.//` rather than `//`: `document.evaluate`
 * ignores its context node for an absolutely-rooted expression, so a `//img`
 * member would collect every image on the page instead of the wrapper's.
 */
function memberCandidates(
  chain: PickSelector[],
  member: { css: string; xpath: string },
  attribute?: string,
): Candidate[] {
  return chainToCandidates(chain, { attribute }).map((candidate) => {
    const kind = candidate.locator.kind === 'xpath' ? 'xpath' : 'css'
    return {
      ...candidate,
      locator: {
        kind,
        selector: kind === 'xpath' ? member.xpath : member.css,
        all: true,
        ...(attribute ? { attribute } : {}),
        within: candidate.locator,
      },
    }
  })
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
 * Ops whose effect the page renders *after* the action returns.
 *
 * `fill` and `clear` put text in an input and it is readable the instant the
 * action returns. A click, a scroll or a tab switch starts work the browser
 * finishes later, and that gap is what a reveal has to be waited out across.
 */
const REVEALING_OPS = new Set<StepOp>([
  'click', 'double_click', 'hover', 'press', 'send_keys', 'select_option',
  'check', 'uncheck', 'scroll', 'scroll_into_view', 'tap', 'swipe', 'drag',
  'find_text', 'new_tab', 'switch_tab', 'close_tab',
])

/** How long a reveal gets to appear before the group reads without it. */
const REVEAL_TIMEOUT_MS = 5_000

/**
 * The document-scoped CSS a field's value lives at, if it has one.
 *
 * A locator is stored relative to its scope -- a table's rows sit inside a
 * container, an array's members inside the picked wrapper -- and a step target
 * is resolved against the document, so the two are composed here rather than
 * one being passed where the other is meant.
 */
function documentScopedSelector(draft: FieldDraft): string | null {
  const rows = draft.repeat?.rows_locator
  // For a table, what appears is the container the rows live in. Waiting on
  // the container rather than a row is deliberate: a drawer can render its
  // list element before it has any children.
  if (rows) {
    if (rows.within?.kind === 'css' && rows.within.selector) return rows.within.selector
    return rows.kind === 'css' ? (rows.selector ?? null) : null
  }
  const dom = draft.candidates.find((c) => c.locator.kind === 'css' && c.locator.selector)
  if (!dom) return null
  const { selector, within } = dom.locator
  if (within?.kind === 'css' && within.selector) return `${within.selector} ${selector}`
  return selector ?? null
}

/**
 * Wait for what the reveal was supposed to reveal, before reading it.
 *
 * **Why this is authored into the recipe rather than done by the engine.** A
 * click that does not navigate returns from the driver the moment the event is
 * dispatched -- `patchright_driver` awaits a new document only when the URL
 * changed -- so a group would otherwise read the page as it was before the
 * drawer opened. The same recipe returned `items: resolved` on one run and
 * `items: empty` on the next against an unchanged page, which is what a race
 * looks like from the outside.
 *
 * The engine could have waited for the DOM to go quiet after every click, and
 * that was the wrong answer twice over. It is an *implicit* wait, so timing
 * stops being reproducible and a recipe that is merely lucky looks correct.
 * And a quiet period is a heuristic that fails hardest exactly where it is
 * needed: a product page with a carousel, a countdown or lazy images never
 * goes quiet, so it would burn the full step timeout on every click and still
 * be free to read too early.
 *
 * The precise condition is available here and nowhere else. The author clicks
 * the reveal and then picks the fields *inside* what it revealed, so the very
 * next field's selector is the thing to wait for -- named explicitly, visible
 * in the document, and editable like any other step. `lint.ts` refuses a bare
 * `wait` where a condition exists; this is that rule applied to its own output.
 *
 * `continue`/`optional` because a page where the content was already open
 * satisfies it immediately, and one where it never appears should report a
 * field it could not read rather than a run that died waiting.
 */
function waitStepFor(draft: FieldDraft): Step | null {
  const selector = documentScopedSelector(draft)
  if (!selector) return null
  return {
    op: 'wait_for_selector',
    target: { kind: 'css', selector },
    args: { state: 'visible' },
    timeout_ms: REVEAL_TIMEOUT_MS,
    on_error: 'continue',
    optional: true,
    label: 'wait for the reveal to render',
  }
}

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
/**
 * One group, as the ordered list implies it.
 *
 * `itemIndices` is what lets the wizard draw the boundaries it is otherwise
 * silent about. Groups are never authored directly -- they fall out of where
 * the actions sit -- and that was invisible in a UI where each group costs a
 * full page load at replay.
 */
export interface PlannedGroup {
  steps: Step[]
  drafts: FieldDraft[]
  /** Indices into the original `items`, so the UI can mark where this starts. */
  itemIndices: number[]
}

export interface GroupPlan {
  /** Items before this index compile into `global_setup`. */
  leadingEnd: number
  globalSetup: Step[]
  groups: PlannedGroup[]
}

/**
 * Work out the groups the ordered list implies, without building a document.
 *
 * Exported because the wizard needs the same answer `itemsToRecipe` computes
 * and must not compute it a second way: a divider drawn from a re-derivation
 * that drifted would tell an author their recipe is shaped one way while the
 * saved document is shaped another. `itemsToRecipe` is a thin wrapper over
 * this for exactly that reason.
 */
export function planGroups(items: WorkItem[]): GroupPlan {
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

  const leadingActions = items.slice(0, leadingEnd).filter((i) => i.kind === 'action')
  const globalSetup = leadingActions.map((i) => i.step)

  // A leading action can be a reveal too -- an author whose first move is
  // "expand the details" puts the click here, not in a group. The condition is
  // the same one a group would wait for: the first field that gets read after
  // it. Harmless when the leading action was only a cookie banner, since the
  // field is already present and the wait resolves at once.
  const lastLeading = leadingActions.at(-1)?.step
  const firstField = firstFieldAt === -1 ? null : items[firstFieldAt]
  if (lastLeading && REVEALING_OPS.has(lastLeading.op) && firstField?.kind === 'field') {
    const wait = waitStepFor(firstField.draft)
    if (wait) globalSetup.push(wait)
  }

  const groups: PlannedGroup[] = []
  let accumulated: Step[] = []
  let current: PlannedGroup | null = null
  // Whether the last action was one whose effect the page renders later.
  let revealed = false

  for (let index = leadingEnd; index < items.length; index++) {
    const item = items[index]
    if (item.kind === 'reset') {
      accumulated = []
      current = null
      revealed = false
      continue
    }
    if (item.kind === 'action') {
      accumulated = [...accumulated, item.step]
      // The next field belongs to a new group: it needs this action, and the
      // fields already placed did not.
      current = null
      revealed = REVEALING_OPS.has(item.step.op)
      continue
    }
    if (current === null) {
      // The reveal has to have *landed* before the group reads. See
      // `waitStepFor`: the driver returns from a click as soon as it is
      // dispatched, so without this the group reads the page as it was before
      // the drawer opened.
      if (revealed) {
        const wait = waitStepFor(item.draft)
        if (wait) accumulated = [...accumulated, wait]
        revealed = false
      }
      current = { steps: accumulated, drafts: [], itemIndices: [] }
      groups.push(current)
    }
    current.drafts.push(item.draft)
    current.itemIndices.push(index)
  }

  return { leadingEnd, globalSetup, groups }
}

export function itemsToRecipe(recipe: Recipe, items: WorkItem[]): Recipe {
  const { globalSetup, groups } = planGroups(items)

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
      // Dropping `within` here meant the preview read a scoped locator against
      // the whole document -- a different question from the one replay asks,
      // and the one thing this reader exists not to do. It went unnoticed while
      // nothing the wizard produced was scoped; an array field is (its members
      // are read inside the picked wrapper), so it would have previewed the
      // page's every element.
      within: toPreviewWithin(c.locator.within),
    }))
}

function toPreviewWithin(within: Locator | undefined): PreviewLocator['within'] {
  if (!within?.selector) return undefined
  if (within.kind !== 'css' && within.kind !== 'xpath') return undefined
  return { kind: within.kind, selector: within.selector }
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
