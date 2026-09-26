/**
 * Record what a person does to the page, as replayable reveal steps.
 *
 * The answer to *"the model could not find it"* that pointing at an element
 * cannot give. Some fields are behind three clicks, a scroll and a dismissal,
 * and no selector describes that sequence -- the assist panel could ask *where
 * is this?* and had no way to ask *how do I get to it?*
 *
 * **The opposite of the picker, deliberately.** `VisualElementPicker` draws an
 * overlay and swallows the click, because it is choosing an element and the
 * page must not react. This must let every event through untouched: the page
 * has to actually open the accordion, or there is nothing to record and the
 * next event is recorded against a state that never existed. So the listeners
 * are passive observers on the capture phase -- no `preventDefault`, no
 * `stopPropagation`, no overlay.
 *
 * Steps come out in the `PreviewStep` shape `preview.ts::runSteps` already
 * accepts, so the panel can rehearse a recording with the code that was already
 * there, and `assist.py` receives the same vocabulary a hand-authored recipe
 * uses.
 *
 * What is deliberately NOT recorded: typing into a field character by character
 * (one `fill` with the final value is what a recipe wants), mouse movement, and
 * anything on the picker's own UI. A recording is meant to be read and edited
 * by a person, and a hundred entries is not.
 */
import { generateRobustSelectors } from './vendor/content/services/dom/domUtils'
import type { PreviewStep } from './preview'
import type { PickPayload } from './protocol'

/**
 * A scroll fires continuously. One step per event would bury the clicks that
 * matter, and the useful fact is "they scrolled here and stopped", which is
 * only knowable once they have.
 */
const SCROLL_QUIET_MS = 400

/** A recording nobody ends must not grow without limit. */
const MAX_STEPS = 60

/**
 * How long to wait before asking what a click revealed.
 *
 * An accordion that animates is open in the DOM long before it has finished
 * moving, so this only has to outlast the handler, not the transition. Long
 * enough for a React state update and a paint; short enough that the settle
 * step lands before the person's next click.
 */
const REVEAL_PROBE_MS = 350

/** Elements belonging to the picker's own overlay, never the page's content. */
const OURS = '[data-testid^="element-picker"], .crawlpilot-test-highlight'

/** Things that are open, and so can be *newly* open after a click. */
const OPENABLE = '[role="dialog"], [role="alertdialog"], dialog[open], details[open]'

/**
 * Controls whose job is to make something go away.
 *
 * Matched on the attributes a close button actually carries, never on class
 * names alone -- on a React page those are generated, and `.close` matching
 * something that merely happens to be called that would mislabel a reveal as a
 * dismissal, which is the one direction that loses data (a dismissal replays
 * `optional`, so a mislabelled reveal fails in silence).
 */
const DISMISS_ATTR = [
  '[aria-label*="close" i]',
  '[aria-label*="dismiss" i]',
  '[aria-label*="reject" i]',
  '[data-dismiss]',
  '[data-testid*="close" i]',
  '[class*="cookie" i] button',
  '[id*="cookie" i] button',
  '[class*="consent" i] button',
  '[id*="consent" i] button',
].join(',')

/** The words on a dismissal, once something dismissable is known to be around. */
const DISMISS_TEXT =
  /^(close|dismiss|accept|accept all|accept cookies|allow all|got it|ok|okay|no thanks|decline|reject|reject all|continue)$/i

/** A bare close glyph. Never anything but a dismissal, whatever contains it. */
const DISMISS_GLYPH = /^[×✕✖⨯x]$/i

function bestCss(el: HTMLElement): string | null {
  const results = generateRobustSelectors(el)
  const css = results.find(
    (r) =>
      !r.selector.startsWith('/') &&
      !r.selector.startsWith('(') &&
      !r.selector.startsWith('./'),
  )
  // CSS only, and not by preference: `steps.py::dispatchability_error` refuses
  // an xpath action target outright, because the driver resolves selectors with
  // `querySelector` and has no XPath engine. Recording one would produce a step
  // that passes validation and then fails on every single run.
  return css ? css.selector : null
}

/** `closest` against a long selector list, without letting one typo end a recording. */
function closest(el: HTMLElement, selector: string): Element | null {
  try {
    return el.closest(selector)
  } catch {
    return null
  }
}

/**
 * Whether this click was getting something out of the way.
 *
 * The distinction is the whole point of intents: `assist.py` replays a
 * dismissal as `optional` -- a cookie banner that did not appear this time is
 * not a failed run -- and that is exactly the wrong treatment for the click
 * that opens the section the field lives in.
 */
export function classifyClick(el: HTMLElement): 'reveal' | 'dismiss' {
  const text = (el.innerText || el.textContent || '').trim()
  if (DISMISS_GLYPH.test(text)) return 'dismiss'
  if (closest(el, DISMISS_ATTR)) return 'dismiss'
  // Text alone is not enough. A page's own "Continue" is a reveal; it only
  // reads as a dismissal when there is something around it to dismiss.
  if (DISMISS_TEXT.test(text) && closest(el, OPENABLE)) return 'dismiss'
  return 'reveal'
}

/**
 * One route entry for a finished pick.
 *
 * Shared rather than a method, because a pick reaches the route two ways and
 * both must produce the same entry: `Recorder.pushSelect`, when the pick was
 * made mid-recording, and the panel appending to a route it has already
 * stopped. Two conversions would drift, and the one that drifted would be the
 * rarer path nobody looks at.
 *
 * An `extract` pick becomes the `select` the binding is read from; a `click`
 * pick becomes an ordinary reveal targeting that element. That is the
 * extension's two interactions -- `ElementDefinition.action` -- with the
 * ordering a recording adds on top.
 */
export function stepForPick(payload: PickPayload): PreviewStep {
  const selector = payload.itemSelector || payload.containerSelector || ''
  const text = (payload.previewValue || payload.value || '').trim().slice(0, 60)
  const target = selector ? { kind: 'css' as const, selector } : {}
  if (payload.action === 'click') {
    return { op: 'click', intent: 'reveal', ...target, text }
  }
  return { op: 'select', intent: 'select', ...target, text, pick: payload }
}

export class Recorder {
  private steps: PreviewStep[] = []
  private scrollTimer: ReturnType<typeof setTimeout> | null = null
  /** Listeners are attached. False while paused for a pick. */
  private running = false
  /** A session exists and its buffer is meaningful. See `isRecording`. */
  private open = false
  private navigated = false
  /**
   * Whether the page changed under the recording.
   *
   * `navigate` is deliberately not recordable -- replay issues its own -- but
   * nothing stopped somebody navigating mid-recording, after which every
   * subsequent step targets a different document. The route then replays
   * against the original page, the later selectors match nothing, and because
   * every reveal step is `optional`/`on_error: continue` it fails in complete
   * silence. Reported so the panel can say to start again rather than letting
   * a broken route be saved.
   */
  get didNavigate(): boolean {
    return this.navigated
  }

  constructor() {
    this.onClick = this.onClick.bind(this)
    this.onChange = this.onChange.bind(this)
    this.onKeyDown = this.onKeyDown.bind(this)
    this.onScroll = this.onScroll.bind(this)
    this.onUnload = this.onUnload.bind(this)
  }

  /**
   * Begin a recording, optionally continuing one the panel already holds.
   *
   * `seed` is what makes a stopped route resumable. Without it the only way
   * back into a recording was to start a fresh one, which discarded the whole
   * route -- so noticing one missing click after pressing Stop meant recording
   * the entire thing again, and any rows that had been deleted, relabelled or
   * reordered in between were lost with it.
   *
   * The panel's copy is the authority, not this one: it is the version that
   * has been edited. Seeding from it rather than keeping the old buffer is
   * what makes Continue preserve those edits.
   */
  start(seed: PreviewStep[] = []): void {
    if (this.running) return
    this.steps = seed.slice(0, MAX_STEPS)
    this.navigated = false
    this.open = true
    this.listen()
  }

  /**
   * Stop observing, but keep everything recorded so far.
   *
   * What makes selection an interaction *within* a route rather than a
   * separate answer beside it. The picker swallows events and this must not
   * see the clicks it swallows, but `start()` clears the buffer, so pausing
   * had to become its own thing -- see `pickInRecording` in `entry.ts`.
   *
   * Navigation stays watched while paused. A page that reloads under a pick is
   * just as fatal to the route as one that reloads under a click.
   */
  pause(): void {
    if (!this.running) return
    this.unlisten()
  }

  /** Pick up where `pause` left off, buffer intact. */
  resume(): void {
    if (this.running || !this.open) return
    this.listen()
  }

  stop(): PreviewStep[] {
    this.unlisten()
    this.open = false
    window.removeEventListener('beforeunload', this.onUnload)
    return this.take()
  }

  private listen(): void {
    this.running = true
    document.addEventListener('click', this.onClick, true)
    document.addEventListener('change', this.onChange, true)
    document.addEventListener('keydown', this.onKeyDown, true)
    document.addEventListener('scroll', this.onScroll, { passive: true, capture: true })
    window.addEventListener('beforeunload', this.onUnload)
  }

  private unlisten(): void {
    document.removeEventListener('click', this.onClick, true)
    document.removeEventListener('change', this.onChange, true)
    document.removeEventListener('keydown', this.onKeyDown, true)
    document.removeEventListener('scroll', this.onScroll, true)
    this.running = false
    if (this.scrollTimer !== null) {
      clearTimeout(this.scrollTimer)
      this.scrollTimer = null
    }
  }

  /** What has been recorded so far, without ending the recording. */
  take(): PreviewStep[] {
    return [...this.steps]
  }

  /**
   * Whether a recording session is open -- listening OR paused for a pick.
   *
   * Deliberately not `running`: a paused recorder still owns its buffer and is
   * still the session the panel is in the middle of, and treating a pause as
   * "not recording" is what would let a second field claim the one in-page
   * `Recorder` and wipe it. See `StepRecorder`'s `recordingField`.
   */
  get isRecording(): boolean {
    return this.open
  }

  /** Open, but not currently observing -- a pick is in flight. */
  get isPaused(): boolean {
    return this.open && !this.running
  }

  /**
   * Whether the cap has been reached and events are being discarded.
   *
   * Silent truncation is the worst shape this failure can take: the person
   * carries on working the page, nothing more is recorded, and the route they
   * submit stops halfway through with no indication of where.
   */
  get isFull(): boolean {
    return this.steps.length >= MAX_STEPS
  }

  /**
   * Fold a finished pick into the route, in the order it was made.
   *
   * An `extract` pick becomes the `select` entry the binding is read from; a
   * `click` pick becomes an ordinary reveal targeting that element. That is
   * the extension's two interactions -- `ElementDefinition.action` -- with the
   * ordering a recording adds on top.
   */
  pushSelect(payload: PickPayload): void {
    this.push(stepForPick(payload))
  }

  private push(step: PreviewStep): void {
    if (this.steps.length >= MAX_STEPS) return
    this.steps.push(step)
  }

  private target(e: Event): HTMLElement | null {
    const el = (e.composedPath()[0] ?? e.target) as HTMLElement | null
    if (!el || !(el instanceof HTMLElement)) return null
    if (el.closest(OURS)) return null
    return el
  }

  private onClick(e: MouseEvent): void {
    const el = this.target(e)
    if (!el) return
    const selector = bestCss(el)
    // Carried for the panel to label the row with, so a recording reads as
    // "click Specifications" rather than as six anonymous selectors.
    const text = (el.innerText || el.textContent || '').trim().slice(0, 60)
    const intent = classifyClick(el)
    // Snapshotted here, in the capture phase, because the whole question is
    // what is open *after* the page handles this click that was not open
    // before it. A moment later is too late.
    const before = intent === 'reveal' ? new Set(document.querySelectorAll(OPENABLE)) : null
    if (!selector) {
      // Recorded WITHOUT a selector rather than dropped.
      //
      // Dropping it was silent in the worst place: the person clicks, nothing
      // appears in the list, and they have no way to know whether the recorder
      // missed it or the click did not register. Worse, "Try these" rehearsed
      // the client-side list while the server ran `parse_recorded_steps` over a
      // shorter one, so a route could rehearse green and be stored with a hole
      // where this click was. A step with no selector fails both the panel's
      // `keepable` check and the server's dispatchability gate, so it is shown
      // struck through and never saved.
      const step: PreviewStep = { op: 'click', intent, text }
      this.push(step)
      if (before) this.armSettle(before, step)
      return
    }
    const step: PreviewStep = { op: 'click', intent, kind: 'css', selector, text }
    this.push(step)
    if (before) this.armSettle(before, step)
  }

  /**
   * Ask, shortly after a reveal click, what it actually revealed -- and record
   * a wait for it.
   *
   * Without this a route clicks the accordion open and reads the field in the
   * same breath. That works when a person does it, because they were never
   * going to beat the animation, and fails on replay, which is. The step is
   * the difference between a route that works and one that works only on the
   * machine it was recorded on.
   *
   * Inserted after the fact rather than reserved up front. A placeholder would
   * be visible to `take()` -- which the panel polls every 700ms -- so a step
   * that may yet turn out not to exist would flicker into the list and out of
   * it. `anchor` is held by identity, not index, because anything at all may
   * have been recorded in between.
   */
  private armSettle(before: Set<Element>, anchor: PreviewStep): void {
    setTimeout(() => {
      // Stopped, or the anchor never made it in past the cap. Either way there
      // is no route for this to belong to.
      if (!this.open || this.isFull) return
      const at = this.steps.indexOf(anchor)
      if (at === -1) return

      let opened: HTMLElement | null = null
      for (const el of document.querySelectorAll(OPENABLE)) {
        if (!before.has(el) && el instanceof HTMLElement) {
          opened = el
          break
        }
      }
      // Nothing this knows how to see opened. The click may still have
      // revealed something -- an accordion that is just a div losing a class
      // -- but guessing a target would be worse than recording no wait at all:
      // a `wait_for_selector` on the wrong element burns its timeout on every
      // single run and then continues anyway.
      if (!opened) return
      const selector = bestCss(opened)
      if (!selector) return

      this.steps.splice(at + 1, 0, {
        op: 'wait_for_selector',
        intent: 'settle',
        kind: 'css',
        selector,
        text: (opened.innerText || '').trim().slice(0, 40),
      })
    }, REVEAL_PROBE_MS)
  }

  private onChange(e: Event): void {
    const el = this.target(e)
    if (!el) return
    const selector = bestCss(el)
    if (!selector) return
    if (el instanceof HTMLSelectElement) {
      this.push({ op: 'select_option', intent: 'reveal', kind: 'css', selector, text: el.value })
      return
    }
    if (el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement) {
      // `change`, not `input`: one step carrying the final value is what a
      // recipe replays. Recording each keystroke would produce a step list
      // nobody can read and a replay that types the same field twelve times.
      this.push({ op: 'fill', intent: 'reveal', kind: 'css', selector, text: el.value })
    }
  }

  private onKeyDown(e: KeyboardEvent): void {
    // Enter and Escape are the two that *do* things -- submitting a search,
    // closing a dialog. The rest is typing, and `change` already has that.
    if (e.key !== 'Enter' && e.key !== 'Escape') return
    // And they are the two intents, in the same order: Enter submits, which
    // the field depends on; Escape closes, which it does not.
    this.push({ op: 'press', intent: e.key === 'Escape' ? 'dismiss' : 'reveal', text: e.key })
  }

  private onUnload(): void {
    this.navigated = true
  }

  private onScroll(): void {
    if (this.scrollTimer !== null) clearTimeout(this.scrollTimer)
    this.scrollTimer = setTimeout(() => {
      this.scrollTimer = null
      const last = this.steps[this.steps.length - 1]
      // Two scrolls in a row are one scroll with a pause in it.
      if (last && last.op === 'scroll') return
      this.push({ op: 'scroll', intent: 'incidental' })
    }, SCROLL_QUIET_MS)
  }
}
