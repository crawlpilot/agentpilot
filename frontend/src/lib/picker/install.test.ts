import { describe, it, expect, beforeAll } from 'vitest'
import fs from 'node:fs'
import path from 'node:path'
import { PICKER_GLOBAL, PICKER_VERSION, type PickerApi, type PickPayload } from './protocol'

/**
 * Guards the injection contract end to end: the *built* bundle, the wrapper it
 * is injected through, and the shape of what comes back.
 *
 * This deliberately tests `generated/picker.iife.js` rather than the modules,
 * because every interesting failure lives in the gap between them. The bundle
 * is committed, so a stale artefact is itself a defect this catches.
 *
 * The wrapper matters most. `execute_js` reaches the page as Playwright's
 * `page.evaluate(script)` (`patchright_driver.py:1412`), which runs the string
 * inside a *function scope* -- so the IIFE's own `var __cpPicker = ...` never
 * becomes a global and the picker would be unreachable on the second call.
 * `entry.ts` assigns to `window` explicitly for that reason, and the wrapper
 * below reproduces the function scope so a regression there fails here.
 */

const BUNDLE = path.resolve(__dirname, 'generated/picker.iife.js')

/** The same wrapper `usePagePicker` installs with. */
const wrap = (bundle: string) =>
  `(function(){ ${bundle}\n; return window.${PICKER_GLOBAL} ? window.${PICKER_GLOBAL}.version : null })()`

function layoutStub() {
  // jsdom gives every element a 0x0 box, and the picker filters on rendered
  // boxes -- without this, nothing is ever considered visible and the
  // container detector returns no siblings.
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
  // jsdom does not implement scrolling; `testSelector` scrolls its first match
  // into view.
  Element.prototype.scrollIntoView = () => {}
}

/**
 * Hover an element the way CDP input would, and let the picker settle.
 *
 * `handleMouseMove` is rAF-throttled, so the picker's `currentElement` -- what
 * a subsequent Enter acts on -- is not set synchronously by the dispatch.
 */
async function hover(el: HTMLElement) {
  document.elementFromPoint = () => el
  el.dispatchEvent(new MouseEvent('mousemove', { bubbles: true, clientX: 50, clientY: 100 }))
  await new Promise((resolve) => requestAnimationFrame(() => setTimeout(resolve, 0)))
}

function install(): PickerApi {
  // Direct `eval` rather than appending a `<script>`: the jsdom environment
  // does not execute injected script tags. What matters for the contract is
  // the *function scope* the wrapper imposes, which this reproduces exactly.
  // Evaluating the built bundle is the point: this test exists to prove the
  // artefact installs itself under the function scope `page.evaluate`
  // imposes. Nothing here is user input.
  // eslint-disable-next-line no-eval
  const version = eval(wrap(fs.readFileSync(BUNDLE, 'utf8')))
  expect(version).toBe(PICKER_VERSION)
  return (window as unknown as Record<string, PickerApi>)[PICKER_GLOBAL]
}

const LIST_HTML = `
  <div id="results"><ul id="l">
    <li class="card"><a href="/a"><img src="/1.jpg"></a><h3 class="t">One</h3><span class="p">$10.00</span></li>
    <li class="card"><a href="/b"><img src="/2.jpg"></a><h3 class="t">Two</h3><span class="p">$20.00</span></li>
    <li class="card"><a href="/c"><img src="/3.jpg"></a><h3 class="t">Three</h3><span class="p">$30.00</span></li>
    <li class="card"><a href="/d"><img src="/4.jpg"></a><h3 class="t">Four</h3><span class="p">$40.00</span></li>
  </ul></div>`

describe('picker bundle installation', () => {
  beforeAll(() => {
    if (!fs.existsSync(BUNDLE)) {
      throw new Error(`Missing ${BUNDLE}. Run \`npm run build:picker\`.`)
    }
    layoutStub()
  })

  it('installs onto window even when evaluated inside a function scope', () => {
    document.body.innerHTML = LIST_HTML
    const api = install()
    expect(api).toBeDefined()
    expect(api.version).toBe(PICKER_VERSION)
  })

  it('exposes the whole protocol surface', () => {
    document.body.innerHTML = LIST_HTML
    const api = install()
    for (const method of [
      'start', 'cancel', 'action', 'take', 'isPicking',
      'showHighlights', 'clearHighlights', 'testSelector',
    ]) {
      expect(typeof api[method as keyof PickerApi], method).toBe('function')
    }
  })

  it('draws and removes its overlay around a picking session', () => {
    document.body.innerHTML = LIST_HTML
    const api = install()
    expect(api.isPicking()).toBe(false)

    api.start('list')
    expect(api.isPicking()).toBe(true)
    expect(document.querySelector('[data-testid=element-picker-overlay]')).not.toBeNull()

    api.cancel()
    expect(api.isPicking()).toBe(false)
    expect(document.querySelector('[data-testid=element-picker-overlay]')).toBeNull()
  })

  it('reports a cancel through the same slot as a selection', () => {
    document.body.innerHTML = LIST_HTML
    const api = install()
    api.start('list')
    expect(api.take()).toBeNull()

    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    expect(api.take()).toEqual({ type: 'PICKER_CANCELLED' })
  })

  it('derives container, item and columns from a repeating list', async () => {
    document.body.innerHTML = LIST_HTML
    const api = install()
    api.start('list')

    await hover(document.querySelectorAll<HTMLElement>('li.card')[1])
    api.action('Enter')

    const msg = api.take()
    expect(msg?.type).toBe('ELEMENT_SELECTED')

    const payload = msg!.payload as PickPayload
    expect(payload.selectionMode).toBe('list')
    expect(payload.patternFound).toBe(true)
    // All four cards, not just the one clicked -- this is the whole point of
    // CommonSelectorGenerator's majority validation.
    expect(payload.count).toBe(4)
    expect(payload.itemSelector).toBeTruthy()
    expect(document.querySelectorAll(payload.itemSelector!)).toHaveLength(4)
    // The ranked chain is what becomes the ordered Candidate[] in v2.
    expect(payload.itemSelectors?.length).toBeGreaterThan(0)
    expect(payload.data?.items.length).toBe(4)
    expect(payload.data?.columns.length).toBeGreaterThan(0)
  })

  it('gives every list column a row-relative locator', async () => {
    document.body.innerHTML = LIST_HTML
    const api = install()
    api.start('list')

    await hover(document.querySelectorAll<HTMLElement>('li.card')[1])
    api.action('Enter')
    const payload = api.take()!.payload as PickPayload

    // The extractor's own `selector` is a path key ("prod > h3"), not CSS.
    // `enrich` is what makes a column addressable, and without it the mapping
    // to a v2 binding has nothing to bind to.
    const columns = payload.data!.columns
    expect(columns.length).toBeGreaterThan(0)
    for (const col of columns) {
      expect(col.locators?.length, `column ${col.name} has locators`).toBeGreaterThan(0)
      // Row-relative: resolvable from inside a row, not only from the document.
      const row = document.querySelector(payload.itemSelector!)!
      expect(row.querySelector(col.locators![0].selector), col.name).not.toBeNull()
    }
  })

  it('recovers the full candidate chain for a detail pick', async () => {
    document.body.innerHTML = `<div><h1 class="title">Widget</h1><span class="price">$42.00</span></div>`
    const api = install()
    api.start('detail')

    await hover(document.querySelector<HTMLElement>('span.price')!)
    api.action('Enter')
    const payload = api.take()!.payload as PickPayload

    expect(payload.extractionType).toBe('text')
    expect(payload.previewValue).toBe('$42.00')
    // Upstream keeps only the best CSS and best XPath; v2 resolves a field by
    // walking an ordered chain, so the discarded ranking is regenerated.
    expect(payload.itemSelectors!.length).toBeGreaterThan(2)
    expect(payload.itemSelectors![0].selector).toBe('span.price')
  })

  it('delivers a result exactly once', async () => {
    document.body.innerHTML = LIST_HTML
    const api = install()
    api.start('list')

    await hover(document.querySelector<HTMLElement>('li.card')!)
    api.action('Enter')

    expect(api.take()).not.toBeNull()
    expect(api.take()).toBeNull()
  })

  it('counts matches for CSS and XPath, and treats an invalid selector as zero', () => {
    document.body.innerHTML = LIST_HTML
    const api = install()
    expect(api.testSelector('li.card')).toBe(4)
    expect(api.testSelector('//li')).toBe(4)
    expect(api.testSelector('#nothing-here')).toBe(0)
    // Must not throw -- an author mid-typing produces invalid selectors
    // constantly, and the studio calls this on every keystroke.
    expect(api.testSelector('bogus$$')).toBe(0)
  })
})
