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
    expect(recorder.stop()).toEqual([{ op: 'scroll' }])
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

  it('stops watching for navigation once stopped', () => {
    const recorder = new Recorder()
    recorder.start()
    recorder.stop()

    window.dispatchEvent(new Event('beforeunload'))

    expect(recorder.didNavigate).toBe(false)
  })
})
