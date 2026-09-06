import { describe, it, expect, beforeAll } from 'vitest'
import fs from 'node:fs'
import path from 'node:path'
import { PICKER_GLOBAL, type PickerApi, type PickPayload } from '@/lib/picker/protocol'
import {
  chainToCandidates,
  detailPickToDraft,
  itemsToRecipe,
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
