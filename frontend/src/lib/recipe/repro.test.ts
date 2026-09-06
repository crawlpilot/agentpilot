import { describe, it, expect, beforeAll } from 'vitest'
import fs from 'node:fs'
import path from 'node:path'
import { PICKER_GLOBAL, type PickerApi, type PickPayload } from '@/lib/picker/protocol'
import { detailPickToDraft } from './fromPick'

const BUNDLE = path.resolve(__dirname, '../picker/generated/picker.iife.js')

const SPEC_HTML = `
  <div id="wrap"><table id="spec"><tbody>
    <tr class="row"><th class="k">Brand</th><td class="v">Nike</td></tr>
    <tr class="row"><th class="k">Colour</th><td class="v">Red</td></tr>
    <tr class="row"><th class="k">Material</th><td class="v">Mesh</td></tr>
    <tr class="row"><th class="k">Weight</th><td class="v">250g</td></tr>
  </tbody></table></div>`

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
  Object.defineProperty(HTMLElement.prototype, 'offsetParent', {
    configurable: true,
    get() { return this.parentElement },
  })
  Element.prototype.scrollIntoView = () => {}
}

function install(): PickerApi {
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
  if (!fs.existsSync(BUNDLE)) throw new Error(`Missing ${BUNDLE}`)
  layoutStub()
})

describe('repro', () => {
  it('detail pick on a spec table', async () => {
    document.body.innerHTML = SPEC_HTML
    const payload = await pick('detail', '#spec')
    console.log('TABLE PAYLOAD', JSON.stringify({
      extractionType: payload.extractionType,
      containerSelector: payload.containerSelector,
      itemSelector: payload.itemSelector,
      itemSelectors: payload.itemSelectors,
      count: payload.count,
      patternFound: payload.patternFound,
      columns: payload.data?.columns,
      items: payload.data?.items,
    }, null, 2))
    const draft = detailPickToDraft(payload)
    console.log('TABLE DRAFT', JSON.stringify(draft, null, 2))
    expect(payload.extractionType).toBe('table')
  })

  it('detail pick on a list container', async () => {
    document.body.innerHTML = LIST_HTML
    const payload = await pick('detail', '#l')
    console.log('LIST PAYLOAD', JSON.stringify({
      extractionType: payload.extractionType,
      containerSelector: payload.containerSelector,
      itemSelector: payload.itemSelector,
      itemSelectors: payload.itemSelectors,
      count: payload.count,
      columns: payload.data?.columns,
      items: payload.data?.items,
    }, null, 2))
    const draft = detailPickToDraft(payload)
    console.log('LIST DRAFT', JSON.stringify(draft, null, 2))
  })
})
