import { describe, it, expect, beforeAll, vi } from 'vitest'
import fs from 'node:fs'
import path from 'node:path'
import { PICKER_GLOBAL, type PickerApi } from './protocol'
import { toRows, type PreviewField, type PreviewResult, type PreviewStep, type StepOutcome } from './preview'

/**
 * The preview is the one screen an author is asked to trust, so its reader is
 * tested against a real DOM rather than assumed correct.
 *
 * Every case here is a way the preview could lie: reading text that replay
 * would not read, ignoring `within`, taking the first match when `index` says
 * otherwise, calling a fallback a clean resolve. Each of those would show
 * green for a recipe that returns garbage in production.
 */

const BUNDLE = path.resolve(__dirname, 'generated/picker.iife.js')

function install(): PickerApi {
  // Evaluating the built bundle is the point: this exercises the artefact
  // that actually gets injected. Nothing here is user input.
  // eslint-disable-next-line no-eval
  eval(`(function(){ ${fs.readFileSync(BUNDLE, 'utf8')} })()`)
  return (window as unknown as Record<string, PickerApi>)[PICKER_GLOBAL]
}

function preview(fields: PreviewField[]): PreviewResult[] {
  return install().preview(fields) as PreviewResult[]
}

const css = (selector: string, extra: Partial<PreviewField['candidates'][0]> = {}) => ({
  kind: 'css' as const,
  selector,
  ...extra,
})

beforeAll(() => {
  if (!fs.existsSync(BUNDLE)) throw new Error(`Missing ${BUNDLE}. Run \`npm run build:picker\`.`)
  Element.prototype.scrollIntoView = () => {}
})

describe('preview reader', () => {
  it('reads text without the contents of inline scripts', () => {
    // This is not hypothetical: Amazon's #availability carries an inline
    // P.when(...) block. Raw textContent would return it as the value --
    // well-formed, plausible, and garbage.
    document.body.innerHTML = `<div id="a">In stock<script>var x = "NOT THIS";</script><style>.c{}</style></div>`
    const [result] = preview([{ name: 'stock', candidates: [css('#a')] }])
    expect(result.value).toBe('In stock')
    expect(result.status).toBe('resolved')
  })

  it('reads an attribute when asked, not the label', () => {
    document.body.innerHTML = `<a id="l" href="/p/1" title="Full title">Short</a>`
    const fields: PreviewField[] = [
      { name: 'text', candidates: [css('#l')] },
      { name: 'link', candidates: [css('#l', { attribute: 'href' })] },
      { name: 'title', candidates: [css('#l', { attribute: 'title' })] },
    ]
    const [text, link, title] = preview(fields)
    expect(text.value).toBe('Short')
    expect(link.value).toBe('/p/1')
    expect(title.value).toBe('Full title')
  })

  it('collects every match with all:true, in document order', () => {
    document.body.innerHTML = `<ul><li class="t">A</li><li class="t">B</li><li class="t">C</li></ul>`
    const [result] = preview([{ name: 'items', candidates: [css('li.t', { all: true })] }])
    expect(result.value).toEqual(['A', 'B', 'C'])
    expect(result.matches).toBe(3)
  })

  it('scopes to the first `within` match only', () => {
    // The engine uses containers[0] (evaluate.py). A preview that searched
    // every container would report more rows than replay ever returns.
    document.body.innerHTML = `
      <div class="box"><span class="v">in</span></div>
      <div class="box"><span class="v">out</span></div>`
    const [result] = preview([
      {
        name: 'v',
        candidates: [css('.v', { all: true, within: { kind: 'css', selector: '.box' } })],
      },
    ])
    expect(result.value).toEqual(['in'])
  })

  it('honours index, including from the end', () => {
    document.body.innerHTML = `<p class="x">1</p><p class="x">2</p><p class="x">3</p>`
    const [second, last] = preview([
      { name: 'second', candidates: [css('p.x', { index: 1 })] },
      { name: 'last', candidates: [css('p.x', { index: -1 })] },
    ])
    expect(second.value).toBe('2')
    expect(last.value).toBe('3')
  })

  it('falls through to the next candidate and says it did', () => {
    document.body.innerHTML = `<span class="real">42</span>`
    const [result] = preview([
      { name: 'price', candidates: [css('.gone'), css('.also-gone'), css('.real')] },
    ])
    // The value is right and the recipe is more fragile than it looks. Both
    // facts have to reach the author.
    expect(result.value).toBe('42')
    expect(result.status).toBe('fallback')
    expect(result.candidate).toBe(3)
  })

  it('treats whitespace-only and empty-list results as not-yielded', () => {
    document.body.innerHTML = `<span class="blank">   </span><span class="real">ok</span>`
    const [result] = preview([{ name: 'v', candidates: [css('.blank'), css('.real')] }])
    expect(result.status).toBe('fallback')
    expect(result.value).toBe('ok')
  })

  it('distinguishes empty, failed and a broken selector', () => {
    document.body.innerHTML = `<div></div>`
    const [empty, failed, broken] = preview([
      { name: 'a', candidates: [css('.nope')] },
      { name: 'b', required: true, candidates: [css('.nope')] },
      { name: 'c', candidates: [css('bogus$$')] },
    ])
    expect(empty.status).toBe('empty')
    expect(failed.status).toBe('failed')
    // A selector the browser cannot parse is a different problem from one
    // that matches nothing, and the author fixes them differently.
    expect(broken.status).toBe('error')
    expect(broken.error).toBeTruthy()
  })

  it('resolves xpath candidates', () => {
    document.body.innerHTML = `<div><b>bold</b></div>`
    const [result] = preview([
      { name: 'v', candidates: [{ kind: 'xpath', selector: '//b' }] },
    ])
    expect(result.value).toBe('bold')
  })
})

describe('toRows', () => {
  it('zips parallel columns into rows', () => {
    const rows = toRows([
      { name: 'title', status: 'resolved', value: ['A', 'B'], candidate: 1, matches: 2 },
      { name: 'price', status: 'resolved', value: ['1', '2'], candidate: 1, matches: 2 },
      { name: 'single', status: 'resolved', value: 'x', candidate: 1, matches: 1 },
    ])
    expect(rows.columns).toEqual(['title', 'price'])
    expect(rows.rows).toEqual([
      ['A', '1'],
      ['B', '2'],
    ])
    expect(rows.aligned).toBe(true)
  })

  it('exposes misalignment rather than hiding it', () => {
    // The column-wise model's one real hazard: a card missing a price yields
    // a short price column, and every value below it is now attached to the
    // wrong row. Surfacing it is the whole reason rows are zipped at all.
    const rows = toRows([
      { name: 'title', status: 'resolved', value: ['A', 'B', 'C'], candidate: 1, matches: 3 },
      { name: 'price', status: 'resolved', value: ['1', '2'], candidate: 1, matches: 2 },
    ])
    expect(rows.aligned).toBe(false)
    expect(rows.counts).toEqual({ title: 3, price: 2 })
    expect(rows.rows).toHaveLength(3)
    expect(rows.rows[2]).toEqual(['C', null])
  })
})

describe('reveal step rehearsal', () => {
  async function apply(steps: PreviewStep[]): Promise<StepOutcome[]> {
    return (await install().applySteps(steps)) as StepOutcome[]
  }

  it('clicks a target and reveals what it controls', async () => {
    document.body.innerHTML = `
      <button id="more">Show</button>
      <div id="panel" style="display:none">hidden value</div>
      <script></script>`
    document.getElementById('more')!.addEventListener('click', () => {
      document.getElementById('panel')!.setAttribute('style', '')
    })

    const before = preview([{ name: 'v', candidates: [css('#panel')] }])
    expect(before[0].value).toBe('hidden value') // in the DOM, just not painted

    const outcomes = await apply([{ op: 'click', selector: '#more', kind: 'css' }])
    expect(outcomes).toEqual([{ op: 'click', status: 'ok' }])
    expect(document.getElementById('panel')!.getAttribute('style')).toBe('')
  })

  it('reports a step whose target is not on the page as skipped, not failed', async () => {
    // The cookie banner that did not appear this time. Not an error.
    document.body.innerHTML = `<div></div>`
    const outcomes = await apply([{ op: 'click', selector: '#absent', kind: 'css' }])
    expect(outcomes[0].status).toBe('skipped')
    expect(outcomes[0].detail).toBe('no match')
  })

  it('fills a field and fires the events a framework listens for', async () => {
    document.body.innerHTML = `<input id="q">`
    const input = document.getElementById('q') as HTMLInputElement
    let sawInput = false
    input.addEventListener('input', () => { sawInput = true })

    const outcomes = await apply([{ op: 'fill', selector: '#q', kind: 'css', text: 'hello' }])
    expect(outcomes[0].status).toBe('ok')
    expect(input.value).toBe('hello')
    // Setting .value alone updates nothing a React/Vue page can see.
    expect(sawInput).toBe(true)
  })

  it('caps a fixed wait so a typo cannot hang the preview', async () => {
    // Fake timers, so asserting the cap costs no wall-clock. A mistyped
    // "60000" must not leave the preview looking hung.
    vi.useFakeTimers()
    try {
      const pending = apply([{ op: 'wait', ms: 60_000 }])
      await vi.advanceTimersByTimeAsync(5_000)
      await expect(pending).resolves.toEqual([{ op: 'wait', status: 'ok' }])
    } finally {
      vi.useRealTimers()
    }
  })
})
