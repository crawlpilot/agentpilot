import { clickElementRobust } from './vendor/content/services/dom/domUtils'
import type { StepIntent } from './protocol'

/**
 * Run the recipe's bindings against the live page and report what comes back.
 *
 * This is the validation step: an author has named some fields and reordered
 * some selectors, and the only question that matters is whether the thing
 * actually yields the data they expect. Everything up to here is a claim about
 * the page; this is the check.
 *
 * **It is a faithful port of `recipe/v2/evaluate.py::_READ_JS` and
 * `resolve.py::resolve_field`, not an approximation.** That is the whole
 * point. A preview that reads elements even slightly differently from replay
 * -- `textContent` instead of the script-stripped walk, first-match instead of
 * `index`, ignoring `within` -- would show green for a recipe that returns
 * garbage in production, which is worse than showing nothing. Where this file
 * looks over-careful, it is matching the engine.
 *
 * What it deliberately does NOT do is apply transforms. Those run server-side
 * (`transform.py`, including Lua) and cannot be reproduced here honestly, so
 * the preview reports the *pre-transform* value and says so. Showing a
 * plausible-looking transformed value computed by different code would be the
 * same lie in a different place.
 */

/** A locator, flattened to what the reader needs. Mirrors `Locator`. */
export interface PreviewLocator {
  kind: 'css' | 'xpath'
  selector: string
  attribute?: string
  all?: boolean
  index?: number | null
  within?: { kind: 'css' | 'xpath'; selector: string }
}

export interface PreviewField {
  name: string
  required?: boolean
  /** In priority order. The first that yields a non-empty value wins. */
  candidates: PreviewLocator[]
}

export type PreviewStatus = 'resolved' | 'fallback' | 'empty' | 'failed' | 'error'

export interface PreviewResult {
  name: string
  status: PreviewStatus
  /** Pre-transform, exactly as the page gave it. */
  value: string | string[] | null
  /** Which candidate produced it, 1-based; null when none did. */
  candidate: number | null
  /** How many nodes the winning selector matched. */
  matches: number
  error?: string
  /** Set when the value came from page JSON rather than the DOM. */
  source?: string
}

/**
 * The in-page reader.
 *
 * A real function, not a source string handed to `new Function`.
 *
 * It was a string, and that was a bug with teeth: `new Function` is `eval` as
 * far as CSP is concerned, so on any page whose policy omits `unsafe-eval` --
 * Amazon and Walmart among them, i.e. exactly the pages this targets -- it
 * threw at module scope, which killed the whole bundle *before* it could
 * assign `window.__cpPicker`. Preview and reveal steps both went silent, and
 * the failure looked like "the preview shows nothing" rather than like a CSP
 * violation. Bundled code has no such problem.
 *
 * This is still a port of `recipe/v2/evaluate.py::_READ_JS` and
 * `resolve.py::resolve_field`, and still deliberately faithful to them; see
 * the module header.
 */

const SKIP_TAGS: Record<string, true> = {
  SCRIPT: true, STYLE: true, NOSCRIPT: true, TEMPLATE: true,
}

/** textContent minus script/style/template subtrees. See the module header. */
function textOf(el: Element): string {
  let out = ''
  const walk = (n: Node) => {
    if (n.nodeType === 3) {
      out += n.nodeValue ?? ''
      return
    }
    if (n.nodeType !== 1 || SKIP_TAGS[(n as Element).tagName]) return
    for (let c = n.firstChild; c; c = c.nextSibling) walk(c)
  }
  walk(el)
  return out
}

function pick(root: Document | Element, selector: string, isXPath: boolean): Element[] | null {
  try {
    if (isXPath) {
      const r = document.evaluate(selector, root, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null)
      const out: Element[] = []
      for (let i = 0; i < r.snapshotLength; i++) {
        const node = r.snapshotItem(i)
        if (node instanceof Element) out.push(node)
      }
      return out
    }
    return Array.from(root.querySelectorAll(selector))
  } catch {
    return null
  }
}

function readValue(el: Element | null | undefined, attribute?: string): string | null {
  if (!el) return null
  const a = attribute || 'text'
  if (a === 'text') return textOf(el).trim()
  if (a === 'visible_text') {
    const t = (el as HTMLElement).innerText
    return t == null ? null : t.trim()
  }
  if (a === 'html') return el.outerHTML ?? null
  if (a === 'value') {
    const v = (el as HTMLInputElement).value
    if (v !== undefined && v !== null) return v
    return el.getAttribute('value')
  }
  return el.getAttribute(a)
}

function evaluateLocator(
  opts: PreviewLocator,
): { value?: string | string[] | null; matches?: number; error?: string } {
  let root: Document | Element = document
  if (opts.within) {
    const containers = pick(document, opts.within.selector, opts.within.kind === 'xpath')
    if (containers === null) return { error: 'invalid within selector' }
    if (!containers.length) return { value: opts.all ? [] : null, matches: 0 }
    root = containers[0]
  }
  const nodes = pick(root, opts.selector, opts.kind === 'xpath')
  if (nodes === null) return { error: 'invalid selector' }
  if (opts.all) {
    return {
      value: nodes.map((n) => readValue(n, opts.attribute)).filter((v): v is string => v !== null),
      matches: nodes.length,
    }
  }
  const i = opts.index === null || opts.index === undefined ? 0 : opts.index
  const el = i < 0 ? nodes[nodes.length + i] : nodes[i]
  return { value: readValue(el, opts.attribute), matches: nodes.length }
}

/**
 * Empty means "did not produce a value", which is what makes the ordered
 * candidate list a fallback chain rather than a list of equals.
 */
function isEmpty(v: unknown): boolean {
  return (
    v === null ||
    v === undefined ||
    (typeof v === 'string' && v.trim() === '') ||
    (Array.isArray(v) && v.length === 0)
  )
}

export function runPreview(fields: PreviewField[]): PreviewResult[] {
  return fields.map((field) => {
    let firstError: string | null = null
    for (let i = 0; i < field.candidates.length; i++) {
      const out = evaluateLocator(field.candidates[i])
      if (out.error) {
        firstError ??= out.error
        continue
      }
      if (isEmpty(out.value)) continue
      return {
        name: field.name,
        status: i === 0 ? 'resolved' : 'fallback',
        value: out.value ?? null,
        candidate: i + 1,
        matches: out.matches ?? 0,
      }
    }
    return {
      name: field.name,
      status: firstError ? 'error' : field.required ? 'failed' : 'empty',
      value: null,
      candidate: null,
      matches: 0,
      error: firstError ?? undefined,
    }
  })
}

/** A table field's rows, read row-wise. Mirrors `dom_rows` replay. */
export interface PreviewRowsField {
  name: string
  rows: { kind: 'css' | 'xpath'; selector: string; within?: { kind: 'css' | 'xpath'; selector: string } }
  /** Column name -> its ordered candidates, each relative to a row. */
  columns: Record<string, PreviewLocator[]>
  maxRows: number
}

export interface PreviewRowsResult {
  name: string
  status: PreviewStatus
  rows: Record<string, string | string[] | null>[]
  /** True when the page had more rows than `maxRows`. */
  truncated: boolean
  /** Column -> which candidate index (1-based) answered, for the first row. */
  candidates: Record<string, number | null>
  error?: string
}

/**
 * Read N rows and resolve every column *relative to its own row*.
 *
 * A faithful port of `recipe/v2/evaluate.py::_READ_ROWS_JS` plus the fallback
 * walk in `replay.py::_rows_from_dom_rows`, for the same reason the scalar
 * reader mirrors `_READ_JS`: a preview that reads rows differently from replay
 * would show green for a recipe that returns something else in production.
 *
 * Rows stay aligned by construction. A column that matches nothing in a given
 * row yields null *for that row* -- it cannot shift the values below it, which
 * is the failure the old column-wise workaround could not avoid.
 */
export function runPreviewRows(fields: PreviewRowsField[]): PreviewRowsResult[] {
  return fields.map((field) => {
    let root: Document | Element = document
    if (field.rows.within) {
      const containers = pick(document, field.rows.within.selector, field.rows.within.kind === 'xpath')
      if (containers === null) {
        return { name: field.name, status: 'error', rows: [], truncated: false, candidates: {}, error: 'invalid within selector' }
      }
      if (!containers.length) {
        return { name: field.name, status: 'empty', rows: [], truncated: false, candidates: {} }
      }
      root = containers[0]
    }

    const rowEls = pick(root, field.rows.selector, field.rows.kind === 'xpath')
    if (rowEls === null) {
      return { name: field.name, status: 'error', rows: [], truncated: false, candidates: {}, error: 'invalid rows selector' }
    }

    const truncated = rowEls.length > field.maxRows
    const candidates: Record<string, number | null> = {}
    const rows = rowEls.slice(0, field.maxRows).map((rowEl, rowIndex) => {
      const row: Record<string, string | string[] | null> = {}
      for (const [column, chain] of Object.entries(field.columns)) {
        let value: string | string[] | null = null
        let won: number | null = null
        for (let i = 0; i < chain.length; i++) {
          const opts = chain[i]
          const nodes = pick(rowEl, opts.selector, opts.kind === 'xpath')
          if (nodes === null || !nodes.length) continue
          const read = opts.all
            ? nodes.map((n) => readValue(n, opts.attribute)).filter((v): v is string => v !== null)
            : readValue(
                (opts.index ?? 0) < 0 ? nodes[nodes.length + (opts.index ?? 0)] : nodes[opts.index ?? 0],
                opts.attribute,
              )
          if (isEmpty(read)) continue
          value = read
          won = i + 1
          break
        }
        row[column] = value
        if (rowIndex === 0) candidates[column] = won
      }
      return row
    })

    return {
      name: field.name,
      status: rows.length === 0 ? 'empty' : truncated ? 'fallback' : 'resolved',
      rows,
      truncated,
      candidates,
    }
  })
}

/** A reveal step, flattened to what the in-page runner needs. */
export interface PreviewStep {
  op: string
  selector?: string
  kind?: 'css' | 'xpath'
  text?: string
  ms?: number
  /**
   * What this entry is for. Absent means `reveal` -- which is what every step
   * recorded before intents existed was in practice. See `StepIntent`.
   */
  intent?: StepIntent
  /** Author-supplied row label, when the recorded text was not descriptive. */
  label?: string
  /**
   * The pick this entry carries. Present only on `intent: 'select'`.
   *
   * Typed `unknown` rather than `PickPayload` so this module stays usable by
   * the studio without dragging the picker's payload shape into it; the two
   * consumers that care (`StepRecorder`, `fromPick`) narrow it themselves.
   */
  pick?: unknown
  /**
   * Which attribute a `select` entry reads — `text`, `href`, `src`, …
   *
   * The picker's classifier guesses one, and the guess is right most of the
   * time and unfixable when it is not. Carried on the step so the route holds
   * the whole answer: the way to the value, the element, and what to read off
   * it. Applied through `setReadAttribute` when the route is converted.
   */
  attribute?: string
}

export interface StepOutcome {
  op: string
  status: 'ok' | 'skipped' | 'failed'
  detail?: string
}

/**
 * Apply the reveal steps in the page, so the preview reads the state the
 * fields actually expect.
 *
 * **This is a rehearsal, not the real thing, and the UI says so.** Replay
 * dispatches a *trusted* CDP click (`Input.dispatchMouseEvent`, via the
 * driver's `_human_click`); this calls `element.click()` from page script.
 * The two are identical to most handlers and different to a few -- anything
 * gated on `event.isTrusted`, and anything that needs real pointer movement
 * first. So a step that works here is not proof it works at replay, though a
 * step that fails here is a genuine problem worth seeing now.
 *
 * The alternative was to leave reveal steps unapplied and let every field
 * behind an accordion preview as `empty`, which teaches the author nothing.
 */
/** How long a `wait_for_selector` with no explicit `ms` waits before giving up. */
const WAIT_FOR_SELECTOR_MS = 3000

export async function runSteps(steps: PreviewStep[]): Promise<StepOutcome[]> {
  const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))
  const out: StepOutcome[] = []

  const pickOne = (selector: string, isXPath: boolean): Element | null => {
    const found = pick(document, selector, isXPath)
    return found && found.length ? found[0] : null
  }

  for (const step of steps) {
    try {
      if (step.op === 'wait') {
        // Capped: a mistyped "60000" must not leave the preview looking hung.
        await sleep(Math.min(step.ms || 0, 5000))
        out.push({ op: step.op, status: 'ok' })
        continue
      }

      if (step.op === 'press') {
        // Targetless by design: a recorded Escape closes whatever has focus,
        // and a recorded Enter submits it. Both are about the page's current
        // state rather than about one element, which is why `Step.press` in the
        // contract carries a key and no target either.
        const key = step.text || 'Enter'
        const to = (document.activeElement as HTMLElement) || document.body
        for (const type of ['keydown', 'keypress', 'keyup']) {
          to.dispatchEvent(
            new KeyboardEvent(type, { key, bubbles: true, cancelable: true }),
          )
        }
        await sleep(300)
        out.push({ op: step.op, status: 'ok' })
        continue
      }

      if (step.op === 'wait_for_selector') {
        // Poll, rather than resolving once against whatever is there right now.
        // A settle step exists precisely because the thing it names is not
        // present yet -- checking for it immediately and reporting `skipped`
        // would make the one step whose job is to wait the one that never does.
        if (!step.selector) {
          out.push({ op: step.op, status: 'skipped', detail: 'no target' })
          continue
        }
        const isXPath = step.kind === 'xpath'
        const until = Date.now() + Math.min(step.ms || WAIT_FOR_SELECTOR_MS, 5000)
        let found = pickOne(step.selector, isXPath)
        while (!found && Date.now() < until) {
          await sleep(100)
          found = pickOne(step.selector, isXPath)
        }
        out.push(
          found
            ? { op: step.op, status: 'ok' }
            : { op: step.op, status: 'skipped', detail: 'never appeared' },
        )
        continue
      }

      const el = step.selector ? pickOne(step.selector, step.kind === 'xpath') : null

      if (step.op === 'scroll') {
        // Pagination's infinite-scroll mode emits this with no target.
        window.scrollTo({ top: document.body.scrollHeight, behavior: 'auto' })
        await sleep(400)
        out.push({ op: step.op, status: 'ok' })
        continue
      }

      if (!el) {
        // The cookie banner that did not appear this time. Not an error.
        out.push({ op: step.op, status: 'skipped', detail: 'no match' })
        continue
      }

      if (step.op === 'click') {
        // `clickElementRobust`, not `el.click()`. A bare `.click()` fires one
        // untrusted `click` and nothing else, which a great many real controls
        // ignore -- anything listening for pointerdown/mousedown, and most
        // component libraries. The vendored helper dispatches the whole
        // pointer/mouse sequence and yields between phases so a framework can
        // process each one, which is why the extension uses it too.
        await clickElementRobust(el as HTMLElement)
        // Let a re-render land before the next step reads the page. Replay has
        // settle/timeout machinery for this; a short pause is the honest
        // approximation of it here.
        await sleep(300)
      } else if (step.op === 'scroll_into_view') {
        el.scrollIntoView({ behavior: 'auto', block: 'center' })
        await sleep(300)
      } else if (step.op === 'fill') {
        const input = el as HTMLInputElement
        input.focus()
        // Assigning `.value` directly is invisible to React, which tracks the
        // last value it wrote on the node. Going through the prototype setter
        // is what makes the framework see the change.
        const proto =
          input instanceof HTMLTextAreaElement
            ? HTMLTextAreaElement.prototype
            : HTMLInputElement.prototype
        const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set
        if (setter) setter.call(input, step.text ?? '')
        else input.value = step.text ?? ''
        input.dispatchEvent(new Event('input', { bubbles: true }))
        input.dispatchEvent(new Event('change', { bubbles: true }))
      } else if (step.op === 'select_option') {
        const select = el as HTMLSelectElement
        select.value = step.text ?? ''
        select.dispatchEvent(new Event('input', { bubbles: true }))
        select.dispatchEvent(new Event('change', { bubbles: true }))
        await sleep(300)
      } else {
        out.push({ op: step.op, status: 'skipped', detail: 'not simulated' })
        continue
      }
      out.push({ op: step.op, status: 'ok' })
    } catch (e) {
      out.push({ op: step.op, status: 'failed', detail: e instanceof Error ? e.message : String(e) })
    }
  }
  return out
}

/**
 * The extracted data as the caller actually receives it.
 *
 * This is the answer to "is the recipe right?", and it is a different question
 * from "did each selector resolve?" -- which is why the preview shows both. A
 * per-field status table can be entirely green while the *shape* is wrong: a
 * table nested where the caller expected a list, a price that is a string with
 * a currency symbol still attached, a column named `span_2`. Rendering the
 * real JSON is the only way that becomes visible before the recipe is saved.
 *
 * Values are pre-transform, for the reason given in the module header, so this
 * is the shape and the raw content -- not the final cast values.
 */
export function toOutputJson(
  results: PreviewResult[],
  rowResults: PreviewRowsResult[] = [],
): Record<string, unknown> {
  const out: Record<string, unknown> = {}
  for (const r of results) {
    // A field that did not resolve is absent from the payload, exactly as
    // `replay.py::_record` leaves it out of `result.data`. Emitting null here
    // would misrepresent what a caller gets.
    if (r.status === 'resolved' || r.status === 'fallback') out[r.name] = r.value
  }
  for (const r of rowResults) {
    if (r.rows.length > 0) out[r.name] = r.rows
  }
  return out
}

/**
 * Zip list-valued results into rows, the way a caller would read them.
 *
 * A repeating list is stored as one `all: true` read per column (see
 * `lib/picker/README.md`), so what comes back is parallel arrays. Rows are
 * what the author actually wants to look at, and -- more usefully -- zipping
 * is what *exposes the misalignment* that model allows: a column that yielded
 * fewer values than its neighbours shows up here as a short column with blanks
 * at the bottom, which is exactly the failure worth catching before saving.
 */
export function toRows(results: PreviewResult[]): {
  columns: string[]
  rows: (string | null)[][]
  /** Per-column value counts; unequal counts mean the rows are not aligned. */
  counts: Record<string, number>
  aligned: boolean
} {
  const listResults = results.filter((r) => Array.isArray(r.value))
  const columns = listResults.map((r) => r.name)
  const counts: Record<string, number> = {}
  for (const r of listResults) counts[r.name] = (r.value as string[]).length

  const height = Math.max(0, ...Object.values(counts))
  const rows: (string | null)[][] = []
  for (let i = 0; i < height; i++) {
    rows.push(listResults.map((r) => (r.value as string[])[i] ?? null))
  }

  const distinct = new Set(Object.values(counts))
  return { columns, rows, counts, aligned: distinct.size <= 1 }
}
