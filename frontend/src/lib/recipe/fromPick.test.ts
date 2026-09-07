import { describe, it, expect, beforeAll } from 'vitest'
import fs from 'node:fs'
import path from 'node:path'
import { PICKER_GLOBAL, type PickerApi, type PickPayload } from '@/lib/picker/protocol'
import type { PreviewResult, PreviewRowsResult } from '@/lib/picker/preview'
import type { ProbeResult } from '@/lib/recipe/probe'
import {
  chainToCandidates,
  toHighlightFields,
  toPreviewFields,
  toPreviewRowsFields,
  detailPickToDraft,
  itemsToRecipe,
  jsonHitToDraft,
  planGroups,
  listPickToDrafts,
  availableShapes,
  readAttribute,
  setShape,
  toFieldName,
  withStructuredPreview,
  withJsonAlternatives,
  type FieldDraft,
  type WorkItem,
} from './fromPick'
import { emptyRecipe, SOURCE_PRIORITY } from './document'
import { lintRecipe } from './lint'
import type { Locator, Step } from './types'

/**
 * The mapping is tested against payloads produced by the *real* picker running
 * over a real DOM, not against hand-written fixtures.
 *
 * A fixture would only ever assert that this file is self-consistent. What can
 * actually break is the seam: the extension's generators changing their chain
 * shape, `enrich` failing to locate a column, a selector that resolves in the
 * picker but not once it is scoped to a row. Driving the built bundle is the
 * only way those show up here rather than in production.
 */

const BUNDLE = path.resolve(__dirname, '../picker/generated/picker.iife.js')

const LIST_HTML = `
  <div id="results"><ul id="l">
    <li class="card" data-testid="prod"><a class="lnk" href="/a"><img class="thumb" src="/1.jpg"></a><h3 class="t">Alpha</h3><span class="p">$10.00</span></li>
    <li class="card" data-testid="prod"><a class="lnk" href="/b"><img class="thumb" src="/2.jpg"></a><h3 class="t">Beta</h3><span class="p">$20.00</span></li>
    <li class="card" data-testid="prod"><a class="lnk" href="/c"><img class="thumb" src="/3.jpg"></a><h3 class="t">Gamma</h3><span class="p">$30.00</span></li>
  </ul></div>`

function layoutStub() {
  let row = 0
  Element.prototype.getBoundingClientRect = function () {
    const top = (row++ % 12) * 90
    return { top, left: 0, width: 200, height: 80, right: 200, bottom: top + 80, x: 0, y: top, toJSON() {} } as DOMRect
  }
  Object.defineProperty(HTMLElement.prototype, 'offsetWidth', { configurable: true, get: () => 200 })
  Object.defineProperty(HTMLElement.prototype, 'offsetHeight', { configurable: true, get: () => 80 })
  Object.defineProperty(HTMLElement.prototype, 'innerText', {
    configurable: true,
    get() { return this.textContent },
    set(v) { this.textContent = v },
  })
  // jsdom never implements `offsetParent`, and `DataExtractor` skips any
  // element with a null one and no direct text -- which is every `<a>` that
  // wraps an image. Without this the url and image columns silently vanish and
  // these tests would be blind to exactly the types most likely to break.
  Object.defineProperty(HTMLElement.prototype, 'offsetParent', {
    configurable: true,
    get() { return this.parentElement },
  })
  Element.prototype.scrollIntoView = () => {}
}

function install(): PickerApi {
  // Evaluating the built bundle is the point: this test exists to prove the
  // artefact installs itself under the function scope `page.evaluate`
  // imposes. Nothing here is user input.
  // eslint-disable-next-line no-eval
  eval(`(function(){ ${fs.readFileSync(BUNDLE, 'utf8')} })()`)
  return (window as unknown as Record<string, PickerApi>)[PICKER_GLOBAL]
}

async function hover(el: HTMLElement) {
  document.elementFromPoint = () => el
  el.dispatchEvent(new MouseEvent('mousemove', { bubbles: true, clientX: 50, clientY: 100 }))
  await new Promise((resolve) => requestAnimationFrame(() => setTimeout(resolve, 0)))
}

async function pick(mode: 'list' | 'detail', selector: string): Promise<PickPayload> {
  const api = install()
  api.start(mode)
  await hover(document.querySelector<HTMLElement>(selector)!)
  api.action('Enter')
  const msg = api.take()
  if (!msg || msg.type !== 'ELEMENT_SELECTED') throw new Error(`no selection for ${selector}`)
  return msg.payload
}

beforeAll(() => {
  if (!fs.existsSync(BUNDLE)) throw new Error(`Missing ${BUNDLE}. Run \`npm run build:picker\`.`)
  layoutStub()
})

describe('toFieldName', () => {
  it('makes a document-safe key', () => {
    expect(toFieldName('Product Title')).toBe('product_title')
    expect(toFieldName('  Price ($) ')).toBe('price')
    expect(toFieldName('!!!')).toBe('field')
  })

  it('never collides with a name already in the document', () => {
    expect(toFieldName('Title', ['title'])).toBe('title_2')
    expect(toFieldName('Title', ['title', 'title_2'])).toBe('title_3')
  })
})

describe('chainToCandidates', () => {
  it('orders by source first, chain position second', () => {
    const candidates = chainToCandidates([
      { selector: '.a', strategy: 'Minimal' },
      { selector: '.b', strategy: 'CSS Gen' },
      { selector: '//x', strategy: 'XPath' },
    ])
    // Every CSS candidate outranks every XPath one, whatever the chain said --
    // priority is the source's, and xpath (70) sorts after css (60).
    expect(candidates.map((c) => c.locator.kind)).toEqual(['css', 'css', 'xpath'])
    expect(candidates[0].priority).toBe(SOURCE_PRIORITY.css)
    expect(candidates[1].priority).toBe(SOURCE_PRIORITY.css + 1)
    expect(candidates[2].priority).toBe(SOURCE_PRIORITY.xpath + 2)
  })

  it('drops positional and hash-class selectors', () => {
    const candidates = chainToCandidates([
      { selector: '.stable', strategy: 'Minimal' },
      { selector: '.x:nth-child(2)', strategy: 'CSS Gen' },
    ])
    expect(candidates.map((c) => c.locator.selector)).toEqual(['.stable'])
  })

  it('keeps a wholly volatile chain rather than yielding no binding', () => {
    const candidates = chainToCandidates([
      { selector: ':nth-child(2)', strategy: 'CSS Gen' },
      { selector: '.a:nth-of-type(1)', strategy: 'CSS Gen' },
    ])
    expect(candidates.length).toBe(2)
  })

  it('marks picked candidates verified, so the lint stays meaningful', () => {
    // A picked selector was derived from a clicked element and, in list mode,
    // validated against its siblings. `verified_on: 0` must keep meaning
    // "nobody has ever seen this work".
    expect(chainToCandidates([{ selector: '.a', strategy: 'Minimal' }])[0].verified_on).toBe(1)
  })

  it('carries attribute and within scoping onto every candidate', () => {
    const within: Locator = { kind: 'css', selector: 'li.card' }
    const candidates = chainToCandidates([{ selector: 'a.lnk', strategy: 'Minimal' }], {
      attribute: 'href',
      within,
    })
    expect(candidates[0].locator.attribute).toBe('href')
    expect(candidates[0].locator.within).toEqual(within)
  })
})

describe('listPickToDrafts', () => {
  it('produces one table field with a dom_rows repeat', async () => {
    document.body.innerHTML = LIST_HTML
    const payload = await pick('list', 'li.card')
    const { drafts, count } = listPickToDrafts(payload)

    expect(count).toBe(3)
    expect(drafts).toHaveLength(1)
    const draft = drafts[0]
    expect(draft.spec.type.kind).toBe('table')
    expect(draft.repeat?.kind).toBe('dom_rows')
    expect(draft.repeat?.row_field).toBe(draft.name)
    // The container is what makes a loose item selector safe.
    expect(draft.repeat?.rows_locator?.within?.selector).toBe(payload.containerSelector)
  })

  it('keeps column selectors row-relative, not composed', async () => {
    document.body.innerHTML = LIST_HTML
    const payload = await pick('list', 'li.card')
    const draft = listPickToDrafts(payload).drafts[0]

    const rows = draft.repeat!.rows_locator!
    const container = document.querySelector(rows.within!.selector!)!
    const rowEls = container.querySelectorAll(rows.selector!)
    expect(rowEls).toHaveLength(3)

    for (const [column, candidates] of Object.entries(draft.columns!)) {
      expect(candidates.length, column).toBeGreaterThan(0)
      const selector = candidates[0].locator.selector!
      // Row-relative: no `all`, no container `within`, resolvable from inside
      // any row. That is the whole simplification dom_rows buys.
      expect(candidates[0].locator.all, column).toBeUndefined()
      expect(candidates[0].locator.within, column).toBeUndefined()
      for (const rowEl of rowEls) {
        expect(rowEl.querySelector(selector), `${column} in every row`).not.toBeNull()
      }
    }
  })

  it('carries a first-row preview per column so it can be named', async () => {
    document.body.innerHTML = LIST_HTML
    const draft = listPickToDrafts(await pick('list', 'li.card')).drafts[0]
    // Several rows, not one: a single sample cannot distinguish "every row
    // says this" from "the first row happened to".
    const previews = Object.values(draft.columnPreviews ?? {})
    expect(previews.some((v) => v.length > 1)).toBe(true)
    expect(draft.columnPreviews?.title ?? draft.columnPreviews?.h3).toEqual(
      expect.arrayContaining(['Alpha', 'Beta']),
    )
  })

  it('declares a column in the type for every binding', async () => {
    document.body.innerHTML = LIST_HTML
    const draft = listPickToDrafts(await pick('list', 'li.card')).drafts[0]
    // The lint keys a table's bindings off `type.columns`; a mismatch reports
    // a correctly-bound column as unresolvable.
    expect(Object.keys(draft.spec.type.columns ?? {}).sort()).toEqual(
      Object.keys(draft.columns!).sort(),
    )
  })
})

describe('detailPickToDraft', () => {
  it('classifies a link as a url read from href, resolved against the page', async () => {
    document.body.innerHTML = `<div><a class="buy" href="/buy">Buy</a></div>`
    const draft = detailPickToDraft(await pick('detail', 'a.buy'))
    expect(draft.spec.type).toEqual({ kind: 'scalar', value_type: 'url' })
    expect(draft.spec.transform).toEqual([{ op: 'url_resolve' }])
    expect(draft.candidates[0].locator.attribute).toBe('href')
  })

  it('classifies an image as a url read from src', async () => {
    document.body.innerHTML = `<div><img class="hero" src="/hero.jpg"></div>`
    const draft = detailPickToDraft(await pick('detail', 'img.hero'))
    expect(draft.spec.type).toEqual({ kind: 'scalar', value_type: 'url' })
    expect(draft.candidates[0].locator.attribute).toBe('src')
  })

  it('classifies plain text and keeps the preview value', async () => {
    document.body.innerHTML = `<div><span class="price">$42.00</span></div>`
    const draft = detailPickToDraft(await pick('detail', 'span.price'))
    expect(draft.spec.type).toEqual({ kind: 'scalar', value_type: 'string' })
    expect(draft.candidates[0].locator.attribute).toBeUndefined()
    expect(draft.preview).toBe('$42.00')
    expect(draft.candidates.length).toBeGreaterThan(1)
  })

  it('reads an array from its members, not from the wrapper', async () => {
    // The clicked element is the gallery; the values are its images. A
    // candidate on the wrapper with `all: true` matches one node and returns a
    // one-item list, which is a list-shaped way of returning nothing.
    document.body.innerHTML = `
      <div class="gal"><img src="/1.jpg"><img src="/2.jpg"><img src="/3.jpg"></div>`
    const draft = detailPickToDraft(await pick('detail', 'div.gal'))
    expect(draft.spec.type.kind).toBe('list')
    const locator = draft.candidates[0].locator
    expect(locator.all).toBe(true)
    expect(locator.selector).toBe('img')
    expect(locator.attribute).toBe('src')
    // The wrapper chain survives as the scope, so a wrapper selector that
    // stops resolving still falls through to the next candidate.
    expect(locator.within?.selector).toBeTruthy()
  })

  it('scopes each member by a wrapper of its own kind, so the field can be saved', async () => {
    // A `css` locator scoped by an `xpath` one is an *error* in the lint and in
    // `validate_document` -- the two are composed rather than resolved
    // separately. Every picked chain ends in XPath candidates, so getting this
    // wrong made an array field unsaveable rather than merely wrong.
    document.body.innerHTML = `
      <div class="gal"><img src="/1.jpg"><img src="/2.jpg"><img src="/3.jpg"></div>`
    const draft = detailPickToDraft(await pick('detail', 'div.gal'))

    expect(draft.candidates.some((c) => c.locator.kind === 'xpath')).toBe(true)
    for (const { locator } of draft.candidates) {
      expect(locator.within?.kind).toBe(locator.kind)
      // `//img` would ignore its context node and collect the whole page;
      // `document.evaluate` only honours the root for a relative expression.
      if (locator.kind === 'xpath') expect(locator.selector?.startsWith('.')).toBe(true)
    }

    const items: WorkItem[] = [{ kind: 'field', id: 'f1', draft }]
    const recipe = itemsToRecipe(emptyRecipe('Gallery'), items)
    expect(lintRecipe(recipe).filter((i) => i.severity === 'error')).toEqual([])
  })

  it('reads a text array as every direct child, through the real reader', async () => {
    // `:scope > *` is only correct if the reader resolves it against the
    // `within` element rather than the document, so this asserts through
    // `api.preview` rather than on the locator alone.
    document.body.innerHTML = `
      <ul class="tags"><li>Alpha</li><li>Beta</li><li>Gamma</li></ul>`
    const draft = detailPickToDraft(await pick('detail', 'ul.tags'))
    expect(draft.spec.type.kind).toBe('list')

    const api = install()
    document.body.innerHTML = `
      <ul class="tags"><li>Alpha</li><li>Beta</li><li>Gamma</li></ul>`
    const [result] = api.preview(toPreviewFields([draft])) as PreviewResult[]
    expect(result.value).toEqual(['Alpha', 'Beta', 'Gamma'])
  })
})

/**
 * A detail pick that lands on a table or a list.
 *
 * This is the commonest way an author reaches a table -- they click the thing
 * they want, and `DetailSelectionStrategy` works out that it repeats. It has
 * to produce the same `dom_rows` field a list-mode pick does, because it is
 * the same data; producing a scalar bound to the container instead is how a
 * twelve-row spec table came back as one string of every cell run together.
 */
/**
 * A broad pick is how you *point at* data; it is not a statement of intent.
 * Clicking a product card finds title, price, image and link, and the reason
 * for clicking it was usually "give me the URLs". These cover narrowing that
 * pick down afterwards, without picking again.
 */
describe('narrowing a broad pick', () => {
  it('flattens one column into a list of values, and it really reads', async () => {
    document.body.innerHTML = LIST_HTML
    const draft = listPickToDrafts(await pick('list', 'li.card')).drafts[0]
    // By what it *reads*, not by value_type: an image column is also `url`.
    const urlColumn = Object.keys(draft.source!.columns).find(
      (c) => readAttribute(draft.source!.columns[c]) === 'href',
    )!
    const flat = setShape(draft, 'values', [urlColumn])

    expect(flat.spec.type).toEqual({ kind: 'list', items: { kind: 'scalar', value_type: 'url' } })
    // A table type with no repeat is a lint error, so both have to go.
    expect(flat.columns).toBeUndefined()
    expect(flat.repeat).toBeUndefined()

    // The composed selector has to resolve against a real page, which is the
    // only claim here worth making -- the object looking right proves nothing.
    const api = install()
    document.body.innerHTML = LIST_HTML
    const [result] = api.preview(toPreviewFields([flat])) as PreviewResult[]
    // Three rows, one value each, in document order. Pre-transform, so the
    // relative href is what the page gave -- `url_resolve` runs server-side.
    expect(result.value).toEqual(['/a', '/b', '/c'])
  })

  it('reads a single value from a column, without the list', async () => {
    document.body.innerHTML = LIST_HTML
    const draft = listPickToDrafts(await pick('list', 'li.card')).drafts[0]
    const titleColumn = Object.keys(draft.source!.columns).find((c) =>
      (draft.source!.columnPreviews[c] ?? []).includes('Alpha'),
    )!
    const one = setShape(draft, 'one', [titleColumn])

    expect(one.spec.type.kind).toBe('scalar')
    expect(one.candidates[0].locator.all).toBeUndefined()

    const api = install()
    document.body.innerHTML = LIST_HTML
    const [result] = api.preview(toPreviewFields([one])) as PreviewResult[]
    expect(result.value).toBe('Alpha')
  })

  it('narrows to a subset of columns and back, losing nothing', async () => {
    document.body.innerHTML = LIST_HTML
    const draft = listPickToDrafts(await pick('list', 'li.card')).drafts[0]
    const all = Object.keys(draft.columns ?? {})
    expect(all.length).toBeGreaterThan(2)

    const narrowed = setShape(draft, 'rows', all.slice(0, 2))
    expect(Object.keys(narrowed.columns ?? {})).toEqual(all.slice(0, 2))
    expect(Object.keys(narrowed.spec.type.columns ?? {})).toEqual(all.slice(0, 2))

    // Round trip: through a flat shape and back to the full table.
    const restored = setShape(setShape(narrowed, 'values', [all[0]]), 'rows', all)
    expect(Object.keys(restored.columns ?? {})).toEqual(all)
    expect(restored.columns).toEqual(draft.columns)
    expect(restored.repeat).toEqual(draft.repeat)
    expect(restored.spec.type).toEqual(draft.spec.type)
  })

  it('compiles a flattened pick into a group with no repeat, and lints clean', async () => {
    document.body.innerHTML = LIST_HTML
    const draft = listPickToDrafts(await pick('list', 'li.card')).drafts[0]
    const urlColumn = Object.keys(draft.source!.columns).find(
      (c) => readAttribute(draft.source!.columns[c]) === 'href',
    )!
    const flat = { ...setShape(draft, 'values', [urlColumn]), name: 'product_urls' }

    const recipe = itemsToRecipe(emptyRecipe('Listing'), [{ kind: 'field', id: 'f', draft: flat }])
    const group = recipe.field_groups[0]

    expect(group.repeat).toBeUndefined()
    expect(group.bindings.product_urls?.length ?? 0).toBeGreaterThan(0)
    // `lint.ts` errors on a table whose group has no repeat -- which is what
    // fires if the projection forgets to clear `columns`.
    expect(lintRecipe(recipe).filter((i) => i.severity === 'error')).toEqual([])
  })

  it('offers only the shapes this pick can actually express', async () => {
    document.body.innerHTML = LIST_HTML
    const draft = listPickToDrafts(await pick('list', 'li.card')).drafts[0]
    const shapes = availableShapes(draft)
    expect(shapes.find((s) => s.shape === 'rows')?.enabled).toBe(true)
    expect(shapes.find((s) => s.shape === 'values')?.enabled).toBe(true)

    // An XPath rows locator cannot be composed with a column selector, so the
    // flat shapes are withheld with a reason rather than silently wrong.
    const xpathRows: typeof draft = {
      ...draft,
      source: {
        ...draft.source!,
        repeat: { ...draft.source!.repeat, rows_locator: { kind: 'xpath', selector: '//li' } },
      },
    }
    const flat = availableShapes(xpathRows).find((s) => s.shape === 'values')!
    expect(flat.enabled).toBe(false)
    expect(flat.reason).toBeTruthy()
  })
})

describe('a detail pick on a repeating container', () => {
  const SPEC_HTML = `
    <div id="wrap"><table id="spec"><tbody>
      <tr class="row"><th class="k">Brand</th><td class="v">Nike</td></tr>
      <tr class="row"><th class="k">Colour</th><td class="v">Red</td></tr>
      <tr class="row"><th class="k">Material</th><td class="v">Mesh</td></tr>
      <tr class="row"><th class="k">Weight</th><td class="v">250g</td></tr>
    </tbody></table></div>`

  it('becomes a dom_rows table, not a scalar read of the container', async () => {
    document.body.innerHTML = LIST_HTML
    const draft = detailPickToDraft(await pick('detail', 'ul#l'))

    expect(draft.spec.type.kind).toBe('table')
    expect(draft.repeat?.kind).toBe('dom_rows')
    // The rows, not the container: scoping to the container would read it once.
    expect(draft.repeat?.rows_locator?.selector).not.toBe('#l')
    expect(draft.repeat?.rows_locator?.within?.selector).toBe('#l')
    expect(draft.repeat?.row_field).toBe(draft.name)
    // Every column carries a binding, and a table binds by column.
    expect(Object.keys(draft.columns ?? {}).length).toBeGreaterThan(1)
    expect(draft.candidates).toEqual([])
  })

  it('resolves the same rows a list-mode pick of the same list would', async () => {
    document.body.innerHTML = LIST_HTML
    const viaDetail = detailPickToDraft(await pick('detail', 'ul#l'))
    document.body.innerHTML = LIST_HTML
    const { drafts } = listPickToDrafts(await pick('list', 'li.card'))

    const api = install()
    document.body.innerHTML = LIST_HTML
    const [fromDetail] = api.previewRows(toPreviewRowsFields([viaDetail])) as PreviewRowsResult[]
    const [fromList] = api.previewRows(toPreviewRowsFields(drafts)) as PreviewRowsResult[]

    expect(fromDetail.rows.length).toBe(3)
    expect(fromDetail.rows.length).toBe(fromList.rows.length)
    // Same values, whichever door the author came through. Column *names* are
    // free to differ -- they are inferred per pick -- so compare the contents.
    const values = (r: PreviewRowsResult) => r.rows.map((row) => Object.values(row).sort())
    expect(values(fromDetail)).toEqual(values(fromList))
  })

  it('reads a th/dt spec table as a label -> value map', async () => {
    document.body.innerHTML = SPEC_HTML
    const draft = detailPickToDraft(await pick('detail', '#spec'))

    expect(draft.keyValue).toBe(true)
    expect(Object.keys(draft.columns ?? {})).toEqual(['name', 'value'])
    // `to_object` is what actually collapses the rows at replay; the type
    // stays `table` because the read really is two columns of rows.
    expect(draft.spec.transform).toEqual([{ op: 'to_object' }])
    expect(draft.spec.type.kind).toBe('table')

    const api = install()
    document.body.innerHTML = SPEC_HTML
    const [rows] = api.previewRows(toPreviewRowsFields([draft])) as PreviewRowsResult[]
    expect(rows.rows).toEqual([
      { name: 'Brand', value: 'Nike' },
      { name: 'Colour', value: 'Red' },
      { name: 'Material', value: 'Mesh' },
      { name: 'Weight', value: '250g' },
    ])
  })

  it('leaves a 2-column list of records alone', async () => {
    // Two columns is what *permits* a map, never what decides one. Only a
    // `th`/`dt` leading the row is evidence, and a product list has neither --
    // collapsing it would key the output on titles.
    document.body.innerHTML = `
      <ul id="p">
        <li class="c"><h3>Alpha</h3><span>$10</span></li>
        <li class="c"><h3>Beta</h3><span>$20</span></li>
        <li class="c"><h3>Gamma</h3><span>$30</span></li>
      </ul>`
    const draft = detailPickToDraft(await pick('detail', 'ul#p'))
    expect(draft.keyValue).toBeFalsy()
    expect(draft.spec.transform).toBeUndefined()
  })

  it('passes the studio lint and binds every column', async () => {
    document.body.innerHTML = SPEC_HTML
    const draft = detailPickToDraft(await pick('detail', '#spec'))
    const items: WorkItem[] = [{ kind: 'field', id: 'f1', draft }]
    const recipe = itemsToRecipe({ ...emptyRecipe('Specs'), sample_urls: [] }, items)

    const group = recipe.field_groups[0]
    expect(group.repeat?.kind).toBe('dom_rows')
    for (const column of Object.keys(recipe.fields[draft.name].type.columns ?? {})) {
      expect(group.bindings[column]?.length ?? 0).toBeGreaterThan(0)
    }
    expect(lintRecipe(recipe).filter((i) => i.severity === 'error')).toEqual([])
  })
})

describe('itemsToRecipe with a picked list', () => {
  it('binds a table by column and passes the studio lint', async () => {
    document.body.innerHTML = LIST_HTML
    const { drafts } = listPickToDrafts(await pick('list', 'li.card'))

    let recipe = emptyRecipe('Search results')
    recipe = {
      ...recipe,
      sample_urls: ['https://e.com/1', 'https://e.com/2', 'https://e.com/3'],
      target: { match: [{ kind: 'glob', pattern: 'https://e.com/*' }] },
    }
    const built = itemsToRecipe(recipe, [{ kind: 'field', id: 'f1', draft: drafts[0] }])

    const group = built.field_groups[0]
    // A table's field name lives in `field_names` and its *columns* key the
    // bindings -- the rule `lint.ts` used to get wrong.
    expect(group.field_names).toEqual([drafts[0].name])
    expect(Object.keys(group.bindings).sort()).toEqual(Object.keys(drafts[0].columns!).sort())
    expect(group.bindings[drafts[0].name]).toBeUndefined()
    expect(group.repeat?.kind).toBe('dom_rows')

    const errors = lintRecipe(built).filter((i) => i.severity === 'error')
    expect(errors, JSON.stringify(errors, null, 2)).toEqual([])
  })

  it('is idempotent for a field that already exists', async () => {
    document.body.innerHTML = LIST_HTML
    const { drafts } = listPickToDrafts(await pick('list', 'li.card'))
    const items: WorkItem[] = [
      { kind: 'field', id: 'f1', draft: drafts[0] },
      { kind: 'field', id: 'f2', draft: drafts[0] },
    ]
    const built = itemsToRecipe(emptyRecipe('r'), items)
    expect(built.field_groups[0].field_names).toEqual([drafts[0].name])
  })
})

describe('withJsonAlternatives', () => {
  it('ranks a structured-data path above the picked CSS candidate', () => {
    const picked = chainToCandidates([{ selector: 'span.price', strategy: 'Minimal' }])
    const merged = withJsonAlternatives(picked, [
      { kind: 'json_ld', path: 'offers.price', path_lang: 'simple' },
    ])
    // This is the whole point: the author clicked rendered text because that
    // is what they could see, and replay still tries the durable path first.
    expect(merged[0].locator.kind).toBe('json_ld')
    expect(merged[merged.length - 1].locator.kind).toBe('css')
    expect(merged[0].priority!).toBeLessThan(merged[merged.length - 1].priority!)
  })

  it('is a no-op when the value is not in the page JSON', () => {
    const picked = chainToCandidates([{ selector: 'span.price', strategy: 'Minimal' }])
    expect(withJsonAlternatives(picked, [])).toEqual(picked)
  })
})

describe('itemsToRecipe', () => {
  const field = (name: string): WorkItem => ({
    kind: 'field',
    id: name,
    draft: {
      name,
      spec: { type: { kind: 'scalar', value_type: 'string' }, description: '' },
      candidates: chainToCandidates([{ selector: `.${name}`, strategy: 'Minimal' }]),
    },
  })
  const action = (id: string, selector: string): WorkItem => ({
    kind: 'action',
    id,
    step: { op: 'click', target: { kind: 'css', selector }, on_error: 'continue' },
  })
  const reset = (id: string): WorkItem => ({ kind: 'reset', id })

  const selectorsOf = (steps: Step[] | undefined) => (steps ?? []).map((s) => s.target?.selector)
  /** Just the actions -- the synthesized reveal waits are asserted separately. */
  const actionsOf = (steps: Step[] | undefined) =>
    (steps ?? []).filter((s) => s.op !== 'wait_for_selector').map((s) => s.target?.selector)

  it('puts actions before the first field into global_setup', () => {
    const r = itemsToRecipe(emptyRecipe('r'), [action('a', '#banner'), field('title')])
    // global_setup is re-run for every group, which is exactly what a cookie
    // banner needs and why leading actions belong there.
    expect(actionsOf(r.global_setup)).toEqual(['#banner'])
    // A leading click can be a reveal as much as a banner dismissal, so it is
    // followed by the same wait a group's would be. Costless when it was only
    // a banner: the field is already there and the wait resolves at once.
    expect(selectorsOf(r.global_setup)).toEqual(['#banner', '.title'])
    expect(r.field_groups).toHaveLength(1)
    expect(r.field_groups[0].field_names).toEqual(['title'])
    expect(r.field_groups[0].steps ?? []).toEqual([])
  })

  it('starts a new group when an action follows a field', () => {
    const r = itemsToRecipe(emptyRecipe('r'), [field('title'), action('a', '#more'), field('origin')])
    expect(r.field_groups).toHaveLength(2)
    expect(r.field_groups[0].field_names).toEqual(['title'])
    expect(r.field_groups[1].field_names).toEqual(['origin'])
    expect(actionsOf(r.field_groups[1].steps)).toEqual(['#more'])
    // ...and the click is followed by a wait for what it reveals.
    expect(selectorsOf(r.field_groups[1].steps)).toEqual(['#more', '.origin'])
  })

  it('accumulates steps, because every group re-navigates', () => {
    // The bug this exists to prevent: emitting only the incremental action.
    // The group holding `c` re-navigated, so `#one`'s effect is gone and it
    // must be replayed before `#two`.
    const r = itemsToRecipe(emptyRecipe('r'), [
      field('a'), action('1', '#one'), field('b'), action('2', '#two'), field('c'),
    ])
    expect(r.field_groups.map((g) => g.field_names)).toEqual([['a'], ['b'], ['c']])
    expect(actionsOf(r.field_groups[1].steps)).toEqual(['#one'])
    expect(actionsOf(r.field_groups[2].steps)).toEqual(['#one', '#two'])
  })

  it('reset clears the accumulation for conflicting reveals', () => {
    // Two drawers that close each other cannot both be open -- the Zara case.
    const r = itemsToRecipe(emptyRecipe('r'), [
      field('a'), action('1', '#drawer-one'), field('b'),
      reset('r1'), action('2', '#drawer-two'), field('c'),
    ])
    expect(actionsOf(r.field_groups[1].steps)).toEqual(['#drawer-one'])
    expect(actionsOf(r.field_groups[2].steps)).toEqual(['#drawer-two'])
  })

  it('waits for what a reveal revealed, before the group reads it', () => {
    // The driver returns from a click as soon as the event is dispatched --
    // it awaits a new document only when the URL changed. Without this the
    // group reads the page as it was before the drawer opened, which showed up
    // as the same recipe resolving a field on one run and not the next.
    const r = itemsToRecipe(emptyRecipe('r'), [field('title'), action('a', '#more'), field('origin')])
    const steps = r.field_groups[1].steps ?? []
    const wait = steps.find((s) => s.op === 'wait_for_selector')

    expect(wait).toBeDefined()
    // The condition is the next field's own selector -- named, not guessed at.
    expect(wait?.target?.selector).toBe('.origin')
    expect(wait?.args?.state).toBe('visible')
    // After the click, never before it.
    expect(steps.indexOf(wait!)).toBe(1)
    // A page where the drawer was already open satisfies it instantly; one
    // where it never opens should report an unreadable field, not a dead run.
    expect(wait?.on_error).toBe('continue')
    expect(wait?.optional).toBe(true)
  })

  it('waits on the container a picked table lives in', () => {
    // A drawer can render its list element before it has any children, so the
    // container is the honest condition -- waiting on a row would race the
    // rows being appended.
    const table: WorkItem = {
      kind: 'field',
      id: 'items',
      draft: {
        name: 'items',
        spec: { type: { kind: 'table', columns: {} }, description: '' },
        candidates: [],
        columns: { t: chainToCandidates([{ selector: '.t', strategy: 'Minimal' }]) },
        repeat: {
          kind: 'dom_rows',
          row_field: 'items',
          max_iterations: 100,
          rows_locator: {
            kind: 'css',
            selector: 'li',
            within: { kind: 'css', selector: 'ul.spec' },
          },
        },
      },
    }
    const r = itemsToRecipe(emptyRecipe('r'), [field('title'), action('a', '#more'), table])
    const wait = (r.field_groups[1].steps ?? []).find((s) => s.op === 'wait_for_selector')
    expect(wait?.target?.selector).toBe('ul.spec')
  })

  it('does not wait after an action that reveals nothing', () => {
    // A `wait` op has already waited; adding a condition after it would be the
    // bare-wait-plus-guess the lint refuses.
    const r = itemsToRecipe(emptyRecipe('r'), [
      field('title'),
      { kind: 'action', id: 'w', step: { op: 'wait', args: { ms: 500 }, on_error: 'continue' } },
      field('origin'),
    ])
    expect((r.field_groups[1].steps ?? []).some((s) => s.op === 'wait_for_selector')).toBe(false)
  })

  it('skips the wait when the next field has no CSS to wait on', () => {
    // A field bound only to a JSON path has no element to become visible, and
    // inventing one would block the group for the full timeout every run.
    const jsonField: WorkItem = {
      kind: 'field',
      id: 'price',
      draft: {
        name: 'price',
        spec: { type: { kind: 'scalar', value_type: 'price' }, description: '' },
        candidates: [
          { priority: 10, locator: { kind: 'json_ld', path: 'offers.price' }, verified_on: 1 },
        ],
      },
    }
    const r = itemsToRecipe(emptyRecipe('r'), [field('title'), action('a', '#more'), jsonField])
    expect(actionsOf(r.field_groups[1].steps)).toEqual(['#more'])
    expect((r.field_groups[1].steps ?? []).some((s) => s.op === 'wait_for_selector')).toBe(false)
  })

  it('plans the same groups the wizard draws', () => {
    // The wizard reads `planGroups` instead of re-deriving the boundaries, so
    // a divider it draws can never disagree with the saved document. This
    // pins the two together.
    const items = [field('a'), action('1', '#one'), field('b'), field('c')]
    const plan = planGroups(items)
    const r = itemsToRecipe(emptyRecipe('r'), items)

    expect(plan.groups).toHaveLength(r.field_groups.length)
    expect(plan.groups.map((g) => g.drafts.map((d) => d.name))).toEqual(
      r.field_groups.map((g) => g.field_names),
    )
    // The indices are what position the dividers: group 2 opens at item 2.
    expect(plan.groups.map((g) => g.itemIndices[0])).toEqual([0, 2])
    // Consecutive fields share a group, so `c` opens nothing.
    expect(plan.groups[1].itemIndices).toEqual([2, 3])
  })

  it('keeps consecutive fields in one group', () => {
    const r = itemsToRecipe(emptyRecipe('r'), [field('a'), field('b'), field('c')])
    expect(r.field_groups).toHaveLength(1)
    expect(r.field_groups[0].field_names).toEqual(['a', 'b', 'c'])
  })

  it('produces a document that passes the lint', () => {
    let recipe = emptyRecipe('Interleaved')
    recipe = {
      ...recipe,
      sample_urls: ['https://e.com/1', 'https://e.com/2', 'https://e.com/3'],
      target: { match: [{ kind: 'glob', pattern: 'https://e.com/*' }] },
    }
    const built = itemsToRecipe(recipe, [
      action('a', '#banner'), field('title'), action('b', '#more'), field('origin'),
    ])
    const errors = lintRecipe(built).filter((i) => i.severity === 'error')
    expect(errors, JSON.stringify(errors, null, 2)).toEqual([])
  })

  it('keeps an empty document the shape emptyRecipe promises', () => {
    const r = itemsToRecipe(emptyRecipe('r'), [])
    expect(r.field_groups).toHaveLength(1)
    expect(r.field_groups[0].field_names).toEqual([])
  })
})


describe('list extraction, end to end', () => {
  it('captures url and image columns, not only text', async () => {
    document.body.innerHTML = LIST_HTML
    const draft = listPickToDrafts(await pick('list', 'li.card')).drafts[0]
    const types = draft.spec.type.columns!

    // Every `<a href>` and `<img src>` in a row is data. Losing them leaves an
    // author with a "list extraction that does not work" for exactly the two
    // types they most often want.
    const valueTypes = Object.values(types).map((t) => t.value_type)
    expect(valueTypes.filter((v) => v === 'url').length).toBeGreaterThanOrEqual(2)

    for (const [name, candidates] of Object.entries(draft.columns!)) {
      if (types[name].value_type !== 'url') continue
      // A url column must READ an attribute -- a link's text is its label.
      expect(['href', 'src'], name).toContain(candidates[0].locator.attribute)
    }
  })

  it('never binds a column to one row\'s own value', async () => {
    document.body.innerHTML = LIST_HTML
    const draft = listPickToDrafts(await pick('list', 'li.card')).drafts[0]

    // `[src="/1.jpg"]` is unique, and useless: it addresses row one's image
    // rather than "the image in this row", so it resolves for the first row
    // and matches nothing below it.
    for (const [name, candidates] of Object.entries(draft.columns!)) {
      for (const c of candidates) {
        const selector = c.locator.selector ?? ''
        expect(selector, `${name}: ${selector}`).not.toMatch(/\[\s*(src|href|id|data-id)\s*=/)
      }
    }
  })

  it('resolves every column in every row, aligned', async () => {
    document.body.innerHTML = LIST_HTML
    const payload = await pick('list', 'li.card')
    const draft = listPickToDrafts(payload).drafts[0]

    const api = install()
    const [table] = api.previewRows(toPreviewRowsFields([draft]) as never[]) as PreviewRowsResult[]

    expect(table.rows).toHaveLength(3)
    for (const row of table.rows) {
      for (const column of Object.keys(draft.columns!)) {
        expect(row[column], `${column} present in every row`).not.toBeNull()
      }
    }
    // Row-wise means the values stay with their own row.
    const textColumn = Object.entries(draft.spec.type.columns!)
      .find(([, t]) => t.value_type === 'string')?.[0]
    if (textColumn) {
      expect(table.rows.map((r) => r[textColumn])).toEqual(['Alpha', 'Beta', 'Gamma'])
    }
  })

  it('marks every column of a picked list on the page', async () => {
    document.body.innerHTML = LIST_HTML
    const draft = listPickToDrafts(await pick('list', 'li.card')).drafts[0]
    const marks = toHighlightFields([draft])

    // A table carries no top-level candidates, so reading `draft.candidates`
    // here returned nothing and a picked list left the page unmarked.
    expect(marks).toHaveLength(Object.keys(draft.columns!).length)
    for (const mark of marks) {
      expect(document.querySelector(mark.selectors[0].value), mark.name).not.toBeNull()
    }
  })
})

/**
 * A recipe built entirely from page JSON is the shape the studio *recommends*
 * -- `SOURCE_PRIORITY` ranks `hydration` above `css` because a path outlives a
 * redesign. It also previewed as blank rows, because the in-page reader reads
 * the DOM and these fields are not in the DOM.
 */
describe('previewing a field bound to page JSON', () => {
  const probe: ProbeResult = {
    json_ld: [],
    metadata: {},
    hydration: {
      __NEXT_DATA__: { props: { pageProps: { initialData: { name: 'Midi dress' } } } },
    },
  }

  const jsonDraft = (name: string, path: string, priority = 15): FieldDraft => ({
    name,
    spec: { type: { kind: 'scalar', value_type: 'string' }, description: '' },
    candidates: [
      { priority, locator: { kind: 'hydration', path, path_lang: 'simple' }, verified_on: 1 },
    ],
  })

  const emptyResult = (name: string): PreviewResult => ({
    name, status: 'empty', value: null, candidate: null, matches: 0,
  })

  it('resolves the value the DOM reader could not', () => {
    const draft = jsonDraft('name', '__NEXT_DATA__.props.pageProps.initialData.name')
    const [out] = withStructuredPreview([emptyResult('name')], [draft], probe)

    expect(out.status).toBe('resolved')
    expect(out.value).toBe('Midi dress')
    // Where it came from, so a green row is not mistaken for a DOM read.
    expect(out.source).toBe('hydration')
  })

  it('leaves a genuinely missing path empty rather than inventing one', () => {
    const draft = jsonDraft('name', '__NEXT_DATA__.props.pageProps.nope.name')
    const [out] = withStructuredPreview([emptyResult('name')], [draft], probe)
    expect(out.status).toBe('empty')
    expect(out.value).toBeNull()
  })

  it('is a no-op before the page has been probed', () => {
    const draft = jsonDraft('name', '__NEXT_DATA__.props.pageProps.initialData.name')
    const results = [emptyResult('name')]
    expect(withStructuredPreview(results, [draft], null)).toEqual(results)
  })

  it('lets a higher-priority DOM candidate keep the answer', () => {
    // `resolve_field` takes the first candidate that yields, in priority
    // order. A CSS candidate that outranks the JSON one must still win here,
    // or the preview shows a value replay would not return.
    const draft: FieldDraft = {
      ...jsonDraft('name', '__NEXT_DATA__.props.pageProps.initialData.name', 60),
      candidates: [
        { priority: 10, locator: { kind: 'css', selector: 'h1' }, verified_on: 1 },
        {
          priority: 60,
          locator: { kind: 'hydration', path: '__NEXT_DATA__.props.pageProps.initialData.name' },
          verified_on: 1,
        },
      ],
    }
    const domWon: PreviewResult = {
      name: 'name', status: 'resolved', value: 'From the DOM', candidate: 1, matches: 1,
    }
    const [out] = withStructuredPreview([domWon], [draft], probe)
    expect(out.value).toBe('From the DOM')
    expect(out.source).toBeUndefined()
  })

  it('outranks a DOM candidate that lost, which is the recommended shape', () => {
    const draft: FieldDraft = {
      ...jsonDraft('name', '__NEXT_DATA__.props.pageProps.initialData.name', 15),
      candidates: [
        {
          priority: 15,
          locator: { kind: 'hydration', path: '__NEXT_DATA__.props.pageProps.initialData.name' },
          verified_on: 1,
        },
        { priority: 60, locator: { kind: 'css', selector: 'h1' }, verified_on: 1 },
      ],
    }
    const [out] = withStructuredPreview([emptyResult('name')], [draft], probe)
    expect(out.value).toBe('Midi dress')
  })
})

describe('jsonHitToDraft', () => {
  it('names the field after the path leaf and outranks CSS', () => {
    const draft = jsonHitToDraft({ kind: 'json_ld', path: 'offers.price', value: '29.99' })
    expect(draft.name).toBe('price')
    expect(draft.preview).toBe('29.99')
    expect(draft.candidates[0].locator.kind).toBe('json_ld')
    // The whole reason to offer this route: a JSON path survives a redesign
    // that breaks every selector on the page, and priority has to say so.
    expect(draft.candidates[0].priority!).toBeLessThan(SOURCE_PRIORITY.css)
    // Read out of this page a moment ago -- not a guess, so the lint's
    // "never verified" warning stays meaningful for the ones that are.
    expect(draft.candidates[0].verified_on).toBe(1)
  })

  it('avoids colliding with a name already taken', () => {
    const a = jsonHitToDraft({ kind: 'meta', path: 'og:title', value: 'x' })
    const b = jsonHitToDraft({ kind: 'hydration', path: 'product.title', value: 'y' }, [a.name])
    expect(b.name).not.toBe(a.name)
  })
})
