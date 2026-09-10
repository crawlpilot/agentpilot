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

/**
 * A scroll fires continuously. One step per event would bury the clicks that
 * matter, and the useful fact is "they scrolled here and stopped", which is
 * only knowable once they have.
 */
const SCROLL_QUIET_MS = 400

/** A recording nobody ends must not grow without limit. */
const MAX_STEPS = 60

/** Elements belonging to the picker's own overlay, never the page's content. */
const OURS = '[data-testid^="element-picker"], .crawlpilot-test-highlight'

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

export class Recorder {
  private steps: PreviewStep[] = []
  private scrollTimer: ReturnType<typeof setTimeout> | null = null
  private running = false

  constructor() {
    this.onClick = this.onClick.bind(this)
    this.onChange = this.onChange.bind(this)
    this.onKeyDown = this.onKeyDown.bind(this)
    this.onScroll = this.onScroll.bind(this)
  }

  start(): void {
    if (this.running) return
    this.running = true
    this.steps = []
    document.addEventListener('click', this.onClick, true)
    document.addEventListener('change', this.onChange, true)
    document.addEventListener('keydown', this.onKeyDown, true)
    document.addEventListener('scroll', this.onScroll, { passive: true, capture: true })
  }

  stop(): PreviewStep[] {
    if (this.running) {
      document.removeEventListener('click', this.onClick, true)
      document.removeEventListener('change', this.onChange, true)
      document.removeEventListener('keydown', this.onKeyDown, true)
      document.removeEventListener('scroll', this.onScroll, true)
      this.running = false
    }
    if (this.scrollTimer !== null) {
      clearTimeout(this.scrollTimer)
      this.scrollTimer = null
    }
    return this.take()
  }

  /** What has been recorded so far, without ending the recording. */
  take(): PreviewStep[] {
    return [...this.steps]
  }

  get isRecording(): boolean {
    return this.running
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
    if (!selector) return
    this.push({
      op: 'click',
      kind: 'css',
      selector,
      // Carried for the panel to label the row with, so a recording reads as
      // "click Specifications" rather than as six anonymous selectors.
      text: (el.innerText || el.textContent || '').trim().slice(0, 60),
    })
  }

  private onChange(e: Event): void {
    const el = this.target(e)
    if (!el) return
    const selector = bestCss(el)
    if (!selector) return
    if (el instanceof HTMLSelectElement) {
      this.push({ op: 'select_option', kind: 'css', selector, text: el.value })
      return
    }
    if (el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement) {
      // `change`, not `input`: one step carrying the final value is what a
      // recipe replays. Recording each keystroke would produce a step list
      // nobody can read and a replay that types the same field twelve times.
      this.push({ op: 'fill', kind: 'css', selector, text: el.value })
    }
  }

  private onKeyDown(e: KeyboardEvent): void {
    // Enter and Escape are the two that *do* things -- submitting a search,
    // closing a dialog. The rest is typing, and `change` already has that.
    if (e.key !== 'Enter' && e.key !== 'Escape') return
    this.push({ op: 'press', text: e.key })
  }

  private onScroll(): void {
    if (this.scrollTimer !== null) clearTimeout(this.scrollTimer)
    this.scrollTimer = setTimeout(() => {
      this.scrollTimer = null
      const last = this.steps[this.steps.length - 1]
      // Two scrolls in a row are one scroll with a pause in it.
      if (last && last.op === 'scroll') return
      this.push({ op: 'scroll' })
    }, SCROLL_QUIET_MS)
  }
}
