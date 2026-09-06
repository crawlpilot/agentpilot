import { describe, it, expect, beforeAll } from 'vitest'
import fs from 'node:fs'
import path from 'node:path'
import { PICKER_GLOBAL, type PickerApi, type PickPayload } from '@/lib/picker/protocol'
import type { PreviewResult, PreviewRowsResult } from '@/lib/picker/preview'
import {
  chainToCandidates,
  toHighlightFields,
  toPreviewFields,
  toPreviewRowsFields,
  detailPickToDraft,
  itemsToRecipe,
  jsonHitToDraft,
  listPickToDrafts,
  toFieldName,
  withJsonAlternatives,
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
    expect(Object.values(draft.columnPreviews ?? {}).some((v) => v.length > 0)).toBe(true)
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

  it('puts actions before the first field into global_setup', () => {
    const r = itemsToRecipe(emptyRecipe('r'), [action('a', '#banner'), field('title')])
    // global_setup is re-run for every group, which is exactly what a cookie
    // banner needs and why leading actions belong there.
    expect(selectorsOf(r.global_setup)).toEqual(['#banner'])
    expect(r.field_groups).toHaveLength(1)
    expect(r.field_groups[0].field_names).toEqual(['title'])
    expect(r.field_groups[0].steps ?? []).toEqual([])
  })

  it('starts a new group when an action follows a field', () => {
    const r = itemsToRecipe(emptyRecipe('r'), [field('title'), action('a', '#more'), field('origin')])
    expect(r.field_groups).toHaveLength(2)
    expect(r.field_groups[0].field_names).toEqual(['title'])
    expect(r.field_groups[1].field_names).toEqual(['origin'])
    expect(selectorsOf(r.field_groups[1].steps)).toEqual(['#more'])
  })

  it('accumulates steps, because every group re-navigates', () => {
    // The bug this exists to prevent: emitting only the incremental action.
    // The group holding `c` re-navigated, so `#one`'s effect is gone and it
    // must be replayed before `#two`.
    const r = itemsToRecipe(emptyRecipe('r'), [
      field('a'), action('1', '#one'), field('b'), action('2', '#two'), field('c'),
    ])
    expect(r.field_groups.map((g) => g.field_names)).toEqual([['a'], ['b'], ['c']])
    expect(selectorsOf(r.field_groups[1].steps)).toEqual(['#one'])
    expect(selectorsOf(r.field_groups[2].steps)).toEqual(['#one', '#two'])
  })

  it('reset clears the accumulation for conflicting reveals', () => {
    // Two drawers that close each other cannot both be open -- the Zara case.
    const r = itemsToRecipe(emptyRecipe('r'), [
      field('a'), action('1', '#drawer-one'), field('b'),
      reset('r1'), action('2', '#drawer-two'), field('c'),
    ])
    expect(selectorsOf(r.field_groups[1].steps)).toEqual(['#drawer-one'])
    expect(selectorsOf(r.field_groups[2].steps)).toEqual(['#drawer-two'])
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
