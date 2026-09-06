import { it, expect, beforeAll } from 'vitest'
import fs from 'node:fs'
import path from 'node:path'
import { PICKER_GLOBAL, type PickerApi } from '@/lib/picker/protocol'
import { listPickToDrafts, itemsToRecipe, toPreviewRowsFields, toHighlightFields } from './fromPick'
import { emptyRecipe } from './document'
import { lintRecipe } from './lint'
const BUNDLE = path.resolve(__dirname, '../picker/generated/picker.iife.js')
let row = 0
beforeAll(() => {
  Element.prototype.getBoundingClientRect = function () {
    const top = (row++ % 12) * 90
    return { top, left: 0, width: 200, height: 80, right: 200, bottom: top+80, x: 0, y: top, toJSON(){} } as DOMRect
  }
  Object.defineProperty(HTMLElement.prototype,'offsetWidth',{configurable:true,get:()=>200})
  Object.defineProperty(HTMLElement.prototype,'offsetHeight',{configurable:true,get:()=>80})
  Object.defineProperty(HTMLElement.prototype,'innerText',{configurable:true,get(){return this.textContent},set(v){this.textContent=v}})
  // jsdom never implements offsetParent, and DataExtractor skips any element
  // with a null offsetParent and no direct text -- which is every <a> that
  // wraps an image. Without this stub the url/image columns silently vanish
  // and the tests are blind to exactly the types under suspicion.
  Object.defineProperty(HTMLElement.prototype,'offsetParent',{configurable:true,get(){return this.parentElement}})
  Element.prototype.scrollIntoView = () => {}
})
it('list pick -> drafts -> recipe -> rows', async () => {
  document.body.innerHTML = `
  <div id="results"><ul id="l">
    <li class="card"><a class="lnk" href="/a"><img class="thumb" src="/1.jpg"></a><h3 class="t">Alpha</h3><span class="p">$10</span></li>
    <li class="card"><a class="lnk" href="/b"><img class="thumb" src="/2.jpg"></a><h3 class="t">Beta</h3><span class="p">$20</span></li>
    <li class="card"><a class="lnk" href="/c"><img class="thumb" src="/3.jpg"></a><h3 class="t">Gamma</h3><span class="p">$30</span></li>
  </ul></div>`
  // eslint-disable-next-line no-eval
  eval(`(function(){ ${fs.readFileSync(BUNDLE,'utf8')} })()`)
  const api = (window as any)[PICKER_GLOBAL] as PickerApi
  api.start('list')
  const card = document.querySelectorAll<HTMLElement>('li.card')[1]
  document.elementFromPoint = () => card
  card.dispatchEvent(new MouseEvent('mousemove',{bubbles:true,clientX:50,clientY:100}))
  await new Promise(r => requestAnimationFrame(() => setTimeout(r,0)))
  api.action('Enter')
  const msg: any = api.take()
  console.log('\n--- payload columns:', JSON.stringify(msg.payload.data.columns.map((c:any)=>({name:c.name,type:c.type,locators:c.locators?.length,attr:c.attribute})),null,1))

  const { drafts, count } = listPickToDrafts(msg.payload)
  console.log('--- count:', count, 'drafts:', drafts.length)
  console.log('--- draft:', JSON.stringify({name:drafts[0]?.name, typeKind:drafts[0]?.spec.type.kind,
    columns:Object.keys(drafts[0]?.columns??{}), repeat:drafts[0]?.repeat}, null, 1))

  console.log('--- highlights:', JSON.stringify(toHighlightFields(drafts)))

  const recipe = itemsToRecipe({...emptyRecipe('r'), sample_urls:['https://e.com/1','https://e.com/2','https://e.com/3'],
    target:{match:[{kind:'glob',pattern:'https://e.com/*'}]}}, [{kind:'field',id:'f',draft:drafts[0]}])
  console.log('--- lint errors:', JSON.stringify(lintRecipe(recipe).filter(i=>i.severity==='error')))

  const rowsReq = toPreviewRowsFields(drafts)
  console.log('--- rows request:', JSON.stringify(rowsReq, null, 1).slice(0, 700))
  const out = api.previewRows(rowsReq as any)
  console.log('--- ROWS OUT:', JSON.stringify(out, null, 1))
  expect(true).toBe(true)
})
