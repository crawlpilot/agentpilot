import { describe, it, expect, beforeAll } from 'vitest'
import fs from 'node:fs'
import path from 'node:path'
import { PICKER_GLOBAL, type PickerApi, type PickPayload } from '@/lib/picker/protocol'
import {
  applyDrafts,
  chainToCandidates,
  detailPickToDraft,
  listPickToDrafts,
  toFieldName,
  withJsonAlternatives,
} from './fromPick'
import { emptyRecipe, SOURCE_PRIORITY } from './document'
import { lintRecipe } from './lint'
import type { Locator } from './types'

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
  it('makes every column a list read scoped to the container', async () => {
    document.body.innerHTML = LIST_HTML
    const payload = await pick('list', 'li.card')
    const { drafts, count } = listPickToDrafts(payload)

    expect(count).toBe(3)
    expect(drafts.length).toBeGreaterThan(0)

    for (const draft of drafts) {
      expect(draft.candidates.length, draft.name).toBeGreaterThan(0)
      expect(draft.spec.type.kind, draft.name).toBe('list')
      const locator = draft.candidates[0].locator
      // `all` is what turns one read into one value per row.
      expect(locator.all, draft.name).toBe(true)
      // Scoped to the container, never the row: `within` resolves to the
      // FIRST match, so a row-scoped read would silently return row one only.
      expect(locator.within?.selector, draft.name).toBe(payload.containerSelector)
    }
  })

  it('reads exactly one value per row, in row order', async () => {
    document.body.innerHTML = LIST_HTML
    const payload = await pick('list', 'li.card')
    const { drafts, count } = listPickToDrafts(payload)

    const container = document.querySelector(payload.containerSelector)!
    for (const draft of drafts) {
      const selector = draft.candidates[0].locator.selector!
      const matches = container.querySelectorAll(selector)
      // The whole column-wise model depends on this: N rows in, N values out.
      // A count that disagrees with the row count is exactly the misalignment
      // that would shift every value after a missing cell.
      expect(matches.length, `${draft.name} -> ${selector}`).toBe(count)
    }
  })

  it('carries a preview value for each column so it can be named', async () => {
    document.body.innerHTML = LIST_HTML
    const payload = await pick('list', 'li.card')
    const { drafts } = listPickToDrafts(payload)
    expect(drafts.some((d) => d.preview && d.preview.length > 0)).toBe(true)
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

describe('applyDrafts', () => {
  it('produces a document that passes the studio lint', async () => {
    document.body.innerHTML = LIST_HTML
    const payload = await pick('list', 'li.card')
    const { drafts } = listPickToDrafts(payload)

    let recipe = emptyRecipe('Search results')
    recipe = {
      ...recipe,
      sample_urls: ['https://example.com/1', 'https://example.com/2', 'https://example.com/3'],
      target: { match: [{ kind: 'glob', pattern: 'https://example.com/*' }] },
    }
    recipe = applyDrafts(recipe, drafts, { groupId: 'rows' })

    const group = recipe.field_groups.find((g) => g.group_id === 'rows')!
    // The three places a field name lives must agree -- that is why applyDrafts
    // writes all of them together.
    for (const draft of drafts) {
      expect(recipe.fields[draft.name]).toBeDefined()
      expect(group.field_names).toContain(draft.name)
      expect(group.bindings[draft.name]?.length).toBeGreaterThan(0)
    }
    const errors = lintRecipe(recipe).filter((issue) => issue.severity === 'error')
    expect(errors, JSON.stringify(errors, null, 2)).toEqual([])
  })

  it('is idempotent for a field that already exists', async () => {
    document.body.innerHTML = LIST_HTML
    const payload = await pick('list', 'li.card')
    const { drafts } = listPickToDrafts(payload)

    const once = applyDrafts(emptyRecipe('r'), drafts, { groupId: 'rows' })
    const twice = applyDrafts(once, drafts, { groupId: 'rows' })
    expect(Object.keys(twice.fields)).toEqual(Object.keys(once.fields))
    const group = twice.field_groups.find((g) => g.group_id === 'rows')!
    expect(group.field_names.length).toBe(drafts.length)
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
