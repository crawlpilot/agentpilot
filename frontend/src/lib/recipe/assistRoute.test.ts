/**
 * `assistRoute` — the state behind one answer on the manual-review screen.
 *
 * Two things are under test, and they are the two ways this screen failed.
 *
 * The first is the ORDER contract: a route is a list of elements somebody
 * pointed at, clicks before the read are how you reach the value and clicks
 * after it are how you tidy up, and the server cuts the array at the same place
 * (`assist.py::split_route`). Get that wrong and a trailing "close this" runs
 * before the binding and shuts the value away.
 *
 * The second is that answering must be something a person DID. The panel used
 * to infer it from its work-in-progress maps, which is why the row replaced its
 * own editor with a summary the moment a route gained an entry, and why Change
 * could not undo it.
 */
import { describe, expect, it } from 'vitest'
import {
  clearAnswer,
  EMPTY,
  forWire,
  isAnswered,
  nextUnanswered,
  resolutionFor,
  routeDraft,
  splitAtRead,
  stepForPick,
  type AskState,
} from './assistRoute'
import type { PickPayload } from '@/lib/picker/protocol'
import type { PendingAsk } from '@/lib/api/types'

function payload(over: Partial<PickPayload> = {}): PickPayload {
  return {
    success: true,
    id: 'sel-1',
    selectionMode: 'detail',
    containerSelector: '.specs',
    itemSelector: '.specs',
    patternFound: false,
    count: 1,
    extractionType: 'text',
    previewValue: 'Weight: 2.4kg',
    action: 'extract',
    ...over,
  } as PickPayload
}

function ask(field: string): PendingAsk {
  return { field, kind: 'unresolved', reason: 'x', step_trace: [] } as PendingAsk
}

describe('stepForPick', () => {
  it('turns a read pick into the marker the binding is taken from', () => {
    const step = stepForPick(payload())
    expect(step.op).toBe('select')
    expect(step.intent).toBe('select')
    expect(step.selector).toBe('.specs')
    expect(step.text).toBe('Weight: 2.4kg')
    // Carried so `routeDraft` can derive locators the same way the standalone
    // pick button does, rather than by a second, poorer path.
    expect(step.pick).toBeTruthy()
  })

  it('turns a click pick into a step that clicks, not a binding', () => {
    // The extension's other interaction -- `ElementDefinition.action` -- and
    // the half this panel could never reach: `pick()` defaults `detail` to
    // `extract`, so pointing at a close button used to read its text into the
    // field instead of scheduling a click on it.
    const step = stepForPick(payload({ action: 'click', itemSelector: '#close' }))
    expect(step.op).toBe('click')
    expect(step.intent).toBe('reveal')
    expect(step.selector).toBe('#close')
    expect(step.pick).toBeUndefined()
  })
})

describe('splitAtRead', () => {
  it('puts clicks before the read in setup and after it in teardown', () => {
    // A group runs its steps and THEN reads, so these cannot be one list.
    const route = [
      { op: 'click', intent: 'dismiss' as const, selector: '#accept' },
      { op: 'click', intent: 'reveal' as const, selector: '#specs' },
      { op: 'select', intent: 'select' as const, selector: 'table', pick: payload() },
      { op: 'click', intent: 'dismiss' as const, selector: '#close' },
    ]
    const { before, read, after } = splitAtRead(route)
    expect(before.map((s) => s.selector)).toEqual(['#accept', '#specs'])
    expect(read?.selector).toBe('table')
    expect(after.map((s) => s.selector)).toEqual(['#close'])
  })

  it('treats a route with no read as all setup', () => {
    const { before, read, after } = splitAtRead([{ op: 'click', selector: '#a' }])
    expect(before).toHaveLength(1)
    expect(read).toBeNull()
    expect(after).toEqual([])
  })

  it('cuts at the LAST read', () => {
    // Somebody who picks, reads the preview and picks again has corrected
    // themselves -- everything between the attempts is still getting there.
    // `assist.py::split_route` makes the same choice.
    const route = [
      { op: 'select', intent: 'select' as const, selector: 'first', pick: payload() },
      { op: 'click', intent: 'reveal' as const, selector: '#wider' },
      { op: 'select', intent: 'select' as const, selector: 'second', pick: payload() },
      { op: 'click', intent: 'dismiss' as const, selector: '#close' },
    ]
    const { before, read, after } = splitAtRead(route)
    expect(before.map((s) => s.selector)).toEqual(['first', '#wider'])
    expect(read?.selector).toBe('second')
    expect(after.map((s) => s.selector)).toEqual(['#close'])
  })
})

describe('routeDraft', () => {
  it('reads the binding out of the route through the ordinary pick path', () => {
    const draft = routeDraft([stepForPick(payload())])
    expect(draft).not.toBeNull()
    expect(draft!.candidates.length).toBeGreaterThan(0)
  })

  it('applies the row attribute override to every candidate', () => {
    // The classifier guesses from the element's kind and its guess used to be
    // final, so a manually picked `<a>` could only ever bind as its text.
    const read = { ...stepForPick(payload()), attribute: 'href' }
    const draft = routeDraft([read])
    expect(draft!.candidates.every((c) => c.locator.attribute === 'href')).toBe(true)
  })

  it('is null for a route of clicks with nothing read', () => {
    expect(routeDraft([{ op: 'click', intent: 'reveal', selector: '#a' }])).toBeNull()
  })
})

describe('forWire', () => {
  it('drops the pick payload, which only the browser needed', () => {
    // The enriched payload runs to tens of kilobytes for a list pick and has
    // already become locators by the time anything is submitted. The server
    // wants the marker so it knows where to cut, and nothing else.
    const step = forWire(stepForPick(payload()))
    expect(step.pick).toBeUndefined()
    expect(step.intent).toBe('select')
    expect(step.op).toBe('select')
  })
})

describe('resolutionFor', () => {
  it('sends nothing while an ask is still being answered', () => {
    // THE bug. A route gaining its first entry used to make the ask answered,
    // which flipped the row to a summary and unmounted the editor that was
    // being used to build it -- leaving no way to finish, stop or undo.
    const building: AskState = { mode: 'route', route: [stepForPick(payload())] }
    expect(resolutionFor('care', building)).toBeNull()
    expect(isAnswered(building)).toBe(false)
  })

  it('sends a route with a read as a pick that carries its own steps', () => {
    const state: AskState = {
      mode: 'done',
      route: [
        { op: 'click', intent: 'reveal', kind: 'css', selector: '#specs' },
        stepForPick(payload()),
        { op: 'click', intent: 'dismiss', kind: 'css', selector: '#close' },
      ],
    }
    const out = resolutionFor('care', state)!
    expect(out.action).toBe('pick')
    expect(out.locators!.length).toBeGreaterThan(0)
    // The read stays in the array: it is the marker the server cuts on, which
    // is how it knows the close click is teardown rather than more setup.
    expect(out.steps).toHaveLength(3)
    expect(out.steps!.every((s) => (s as { pick?: unknown }).pick === undefined)).toBe(true)
  })

  it('sends a route of clicks alone as steps for the model to look at', () => {
    const state: AskState = {
      mode: 'done',
      route: [{ op: 'click', intent: 'reveal', kind: 'css', selector: '#specs' }],
    }
    expect(resolutionFor('care', state)!.action).toBe('steps')
  })

  it('passes a committed answer through untouched', () => {
    const state: AskState = {
      mode: 'done',
      route: [],
      answer: { field: 'care', action: 'skip' },
    }
    expect(resolutionFor('care', state)!.action).toBe('skip')
  })
})

describe('clearAnswer', () => {
  it('returns the ask all the way to unanswered', () => {
    // Change used to prune three of four per-field maps and leave the route
    // behind, so the answer was re-derived from it and the row snapped shut
    // again -- a dead end with no way back to the mechanism buttons.
    const answered: AskState = {
      mode: 'done',
      route: [stepForPick(payload())],
      pick: { candidates: [], spec: {} as never, preview: 'x' },
      scope: { selector: '.s', matched: 1, preview: 'y' },
      answer: { field: 'care', action: 'skip' },
    }
    const cleared = clearAnswer()
    expect(cleared).toEqual(EMPTY)
    expect(cleared.route).toEqual([])
    expect(resolutionFor('care', cleared)).toBeNull()
    expect(isAnswered(answered)).toBe(true)
    expect(isAnswered(cleared)).toBe(false)
  })
})

describe('nextUnanswered', () => {
  const asks = [ask('a'), ask('b'), ask('c')]
  const done: AskState = { mode: 'done', route: [], answer: { field: 'x', action: 'skip' } }

  it('moves on to the next ask still wanting an answer', () => {
    expect(nextUnanswered(asks, { a: done }, 'a')).toBe('b')
  })

  it('skips ones already answered, and wraps', () => {
    expect(nextUnanswered(asks, { a: done, b: done }, 'a')).toBe('c')
    expect(nextUnanswered(asks, { b: done, c: done }, 'c')).toBe('a')
  })

  it('stays put when everything is answered, rather than selecting nothing', () => {
    expect(nextUnanswered(asks, { a: done, b: done, c: done }, 'b')).toBe('b')
  })
})
