/**
 * `record.ts` — the route to a field, recorded from what a person actually did.
 *
 * Two things are under test and they are the two ways this can be useless: the
 * steps must come out in the shape `runSteps` and `assist.py` already accept,
 * and the recording must not swallow the events it observes — the page has to
 * genuinely open the accordion, or the next event is recorded against a state
 * that never existed.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { Recorder } from './record'

// Spied on rather than replaced: every other test wants the real generator, and
// only the no-stable-selector case needs to force its answer.
vi.mock('./vendor/content/services/dom/domUtils', async (importOriginal) => {
  const actual = await importOriginal<
    typeof import('./vendor/content/services/dom/domUtils')
  >()
  return { ...actual, generateRobustSelectors: vi.fn(actual.generateRobustSelectors) }
})

function html(markup: string) {
  document.body.innerHTML = markup
}

describe('Recorder', () => {
  beforeEach(() => {
    document.body.innerHTML = ''
    vi.useRealTimers()
  })

  it('records a click as a replayable step', () => {
    html('<button id="specs">Specifications</button>')
    const recorder = new Recorder()
    recorder.start()

    document.getElementById('specs')!.click()
    const steps = recorder.stop()

    expect(steps).toHaveLength(1)
    expect(steps[0].op).toBe('click')
    expect(steps[0].kind).toBe('css')
    expect(steps[0].selector).toBeTruthy()
    // Carried so the panel can label the row with what they clicked, rather
    // than with an anonymous selector.
    expect(steps[0].text).toBe('Specifications')
  })

  it('lets the event through, because the page has to actually react', () => {
    html('<button id="go">Open</button>')
    const seen = vi.fn()
    document.getElementById('go')!.addEventListener('click', seen)

    const recorder = new Recorder()
    recorder.start()
    document.getElementById('go')!.click()
    recorder.stop()

    // The whole difference from the picker, which swallows the click on
    // purpose. Here a swallowed click means the accordion never opens and
    // everything recorded after it is against a state that never existed.
    expect(seen).toHaveBeenCalledOnce()
  })

  it('records a filled input once, with its final value', () => {
    html('<input id="q" />')
    const input = document.getElementById('q') as HTMLInputElement
    const recorder = new Recorder()
    recorder.start()

    input.value = 'wireless mouse'
    input.dispatchEvent(new Event('change', { bubbles: true }))
    const steps = recorder.stop()

    // `change`, not `input`: one step carrying the final value is what a recipe
    // replays. Per-keystroke steps would be unreadable and would replay the
    // same field a dozen times.
    expect(steps).toEqual([
      expect.objectContaining({ op: 'fill', text: 'wireless mouse' }),
    ])
  })

  it('records a select by its chosen value', () => {
    html('<select id="s"><option value="S">S</option><option value="M">M</option></select>')
    const select = document.getElementById('s') as HTMLSelectElement
    const recorder = new Recorder()
    recorder.start()

    select.value = 'M'
    select.dispatchEvent(new Event('change', { bubbles: true }))

    expect(recorder.stop()).toEqual([
      expect.objectContaining({ op: 'select_option', text: 'M' }),
    ])
  })

  it('records only the keys that do something', () => {
    html('<input id="q" />')
    const input = document.getElementById('q')!
    const recorder = new Recorder()
    recorder.start()

    for (const key of ['a', 'b', 'Shift', 'Enter', 'Escape']) {
      input.dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true }))
    }

    // Enter submits and Escape closes; the rest is typing, and `change`
    // already has that.
    expect(recorder.stop().map((s) => s.text)).toEqual(['Enter', 'Escape'])
  })

  it('collapses a burst of scrolling into one step', async () => {
    vi.useFakeTimers()
    const recorder = new Recorder()
    recorder.start()

    for (let i = 0; i < 20; i++) {
      document.dispatchEvent(new Event('scroll'))
    }
    vi.advanceTimersByTime(500)

    // The useful fact is "they scrolled here and stopped", which is only
    // knowable once they have. Twenty steps would bury the clicks that matter.
    // `incidental` because that is what scrolling to look at something is --
    // it reads like what the person did, and dropping it costs the route
    // nothing.
    expect(recorder.stop()).toEqual([{ op: 'scroll', intent: 'incidental' }])
    vi.useRealTimers()
  })

  it('ignores the picker\'s own overlay', () => {
    html('<div data-testid="element-picker-overlay"><span id="inner">x</span></div>')
    const recorder = new Recorder()
    recorder.start()

    document.getElementById('inner')!.click()

    expect(recorder.stop()).toEqual([])
  })

  it('keeps recording available until it is stopped', () => {
    html('<button id="a">A</button><button id="b">B</button>')
    const recorder = new Recorder()
    recorder.start()

    document.getElementById('a')!.click()
    // `take` is what the panel polls so the person can watch steps arrive.
    expect(recorder.take()).toHaveLength(1)
    expect(recorder.isRecording).toBe(true)

    document.getElementById('b')!.click()
    expect(recorder.stop()).toHaveLength(2)
    expect(recorder.isRecording).toBe(false)
  })

  it('stops listening once stopped', () => {
    html('<button id="a">A</button>')
    const recorder = new Recorder()
    recorder.start()
    recorder.stop()

    document.getElementById('a')!.click()

    expect(recorder.take()).toEqual([])
  })

  it('starting again discards the previous recording', () => {
    html('<button id="a">A</button>')
    const recorder = new Recorder()
    recorder.start()
    document.getElementById('a')!.click()
    recorder.stop()

    recorder.start()
    expect(recorder.take()).toEqual([])
  })

  it('continues a stopped route from what the panel holds', () => {
    // Stopping used to be final: noticing one missed click meant Record again,
    // which wiped the route -- including every row deleted, relabelled or
    // reordered since. Seeding from the PANEL's copy rather than keeping the
    // old buffer is what makes those edits survive the continue.
    html('<button id="a">A</button><button id="b">B</button>')
    const recorder = new Recorder()
    recorder.start()
    document.getElementById('a')!.click()
    const first = recorder.stop()
    expect(first).toHaveLength(1)

    // The panel relabelled the row while it was stopped.
    const edited = [{ ...first[0], label: 'open the specs' }]
    recorder.start(edited)
    document.getElementById('b')!.click()

    const steps = recorder.stop()
    expect(steps).toHaveLength(2)
    expect(steps[0].label).toBe('open the specs')
    expect(steps[1].text).toBe('B')
  })

  it('will not seed a route past the cap', () => {
    const recorder = new Recorder()
    recorder.start(Array.from({ length: 80 }, () => ({ op: 'click' })))
    expect(recorder.take()).toHaveLength(60)
    expect(recorder.isFull).toBe(true)
  })

  it('marks a click it could not give a selector, rather than dropping it', async () => {
    // Dropping it was silent in the worst possible place: the person clicks,
    // nothing appears in the list, and there is no way to tell a missed
    // recording from a click that never registered. Worse, "Try these"
    // rehearsed the client-side list while `parse_recorded_steps` stored a
    // shorter one -- so a route could rehearse green and be saved with a hole
    // in it. A step with no selector fails the panel's `keepable` check and the
    // server's dispatchability gate, so it is shown and never saved.
    //
    // `bestCss` rejects XPath outright, because `dispatchability_error` refuses
    // an xpath action target -- so an element whose only robust address is an
    // XPath is exactly the case that produced no selector.
    const { generateRobustSelectors } = await import(
      './vendor/content/services/dom/domUtils'
    )
    const only = vi
      .mocked(generateRobustSelectors)
      .mockReturnValue([{ selector: '//button[1]', strategy: 'XPath' }] as never)

    html('<button id="target">Specifications</button>')
    const recorder = new Recorder()
    recorder.start()
    document.getElementById('target')!.click()
    const steps = recorder.take()
    only.mockRestore()

    expect(steps).toHaveLength(1)
    expect(steps[0].op).toBe('click')
    expect(steps[0].selector).toBeUndefined()
    // The label survives, so the panel can say WHICH click was not captured.
    expect(steps[0].text).toBe('Specifications')
  })

  it('reports a navigation that happened under the recording', () => {
    // `navigate` is not recordable on purpose -- replay issues its own -- but
    // every step after a navigation targets a different document, and because
    // reveal steps are all `optional`/`on_error: continue` the resulting route
    // fails in complete silence.
    html('<button id="a">A</button>')
    const recorder = new Recorder()
    recorder.start()
    expect(recorder.didNavigate).toBe(false)

    window.dispatchEvent(new Event('beforeunload'))

    expect(recorder.didNavigate).toBe(true)
  })

  it('tells a dismissal apart from a reveal', () => {
    // The distinction the intents exist for. `assist.py` replays a dismissal
    // as `optional` -- a banner that did not appear is not a failed run -- and
    // that is exactly the wrong treatment for the click that opens the section
    // the field lives in, which then fails in silence.
    html(`
      <div id="cookie"><button id="accept">Accept all</button></div>
      <button id="x" aria-label="Close dialog">✕</button>
      <button id="specs">Specifications</button>
      <button id="go">Continue</button>
    `)
    const recorder = new Recorder()
    recorder.start()

    document.getElementById('accept')!.click()
    document.getElementById('x')!.click()
    document.getElementById('specs')!.click()
    // Text alone is not enough: a page's own "Continue" is a reveal, and only
    // reads as a dismissal when there is something around it to dismiss.
    document.getElementById('go')!.click()

    expect(recorder.stop().map((s) => s.intent)).toEqual([
      'dismiss',
      'dismiss',
      'reveal',
      'reveal',
    ])
  })

  it('keeps the buffer across a pause, where start clears it', () => {
    // What makes selection an interaction *within* a route rather than a
    // separate answer beside it. The picker swallows the clicks it draws over,
    // so the recorder has to stop listening -- but `start()` clears the
    // buffer, so pausing had to become its own thing.
    html('<button id="a">A</button><button id="b">B</button>')
    const recorder = new Recorder()
    recorder.start()
    document.getElementById('a')!.click()

    recorder.pause()
    expect(recorder.take()).toHaveLength(1)
    expect(recorder.isPaused).toBe(true)
    // Still the panel's session, so a second field cannot claim the one
    // in-page recorder and wipe it.
    expect(recorder.isRecording).toBe(true)

    // Nothing reaches the buffer while the picker owns the page.
    document.getElementById('b')!.click()
    expect(recorder.take()).toHaveLength(1)

    recorder.resume()
    document.getElementById('b')!.click()
    expect(recorder.take()).toHaveLength(2)
  })

  it('resumes from a hand pause exactly where it left off', () => {
    // Distinct from the pause a pick takes, and needed for a different reason:
    // finding the thing worth recording usually means clicking around first,
    // and every one of those clicks would otherwise land in the route.
    html('<button id="a">A</button><button id="b">B</button>')
    const recorder = new Recorder()
    recorder.start()
    document.getElementById('a')!.click()

    recorder.pause()
    for (let i = 0; i < 5; i++) document.getElementById('b')!.click()
    expect(recorder.take()).toHaveLength(1)

    recorder.resume()
    document.getElementById('b')!.click()

    const steps = recorder.stop()
    expect(steps).toHaveLength(2)
    expect(steps[1].text).toBe('B')
  })

  it('will not resume a recording that was stopped', () => {
    // `resume` is about a session that is still open. Reviving a stopped one
    // would reattach listeners feeding a buffer the panel has already taken
    // and submitted.
    html('<button id="a">A</button>')
    const recorder = new Recorder()
    recorder.start()
    recorder.stop()

    recorder.resume()
    document.getElementById('a')!.click()

    expect(recorder.isRecording).toBe(false)
    expect(recorder.take()).toEqual([])
  })

  it('folds a pick into the route in the order it was made', () => {
    html('<button id="a">A</button><button id="b">B</button>')
    const recorder = new Recorder()
    recorder.start()
    document.getElementById('a')!.click()

    recorder.pause()
    recorder.pushSelect({
      itemSelector: 'table.specs',
      previewValue: 'Weight: 2.4kg',
      action: 'extract',
    } as never)
    recorder.resume()
    document.getElementById('b')!.click()

    const steps = recorder.stop()
    expect(steps.map((s) => s.intent)).toEqual(['reveal', 'select', 'reveal'])
    expect(steps[1].selector).toBe('table.specs')
    expect(steps[1].text).toBe('Weight: 2.4kg')
  })

  it('records a pick made in click mode as a reveal, not a binding', () => {
    // The extension's other half -- `ElementDefinition.action` -- and until
    // now unreachable from this panel: `pick()` has always defaulted `detail`
    // to `extract`. Pointing at a close button should schedule a click on it,
    // not read its text into the field.
    const recorder = new Recorder()
    recorder.start()
    recorder.pause()
    recorder.pushSelect({ itemSelector: '#close', action: 'click' } as never)

    const steps = recorder.stop()
    expect(steps).toHaveLength(1)
    expect(steps[0].op).toBe('click')
    expect(steps[0].intent).toBe('reveal')
    expect(steps[0].pick).toBeUndefined()
  })

  it('reports the cap rather than truncating in silence', () => {
    // The person carries on working the page, nothing more is recorded, and
    // the route they submit stops halfway through with no indication where.
    html('<button id="a">A</button>')
    const recorder = new Recorder()
    recorder.start()
    expect(recorder.isFull).toBe(false)

    for (let i = 0; i < 70; i++) document.getElementById('a')!.click()

    expect(recorder.isFull).toBe(true)
    expect(recorder.take()).toHaveLength(60)
  })

  it('stops watching for navigation once stopped', () => {
    const recorder = new Recorder()
    recorder.start()
    recorder.stop()

    window.dispatchEvent(new Event('beforeunload'))

    expect(recorder.didNavigate).toBe(false)
  })
})
