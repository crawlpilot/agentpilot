/**
 * The in-page half of the visual picker.
 *
 * This file is NOT bundled into the app. `vite.picker.config.ts` builds it
 * alone into a self-contained IIFE at `generated/picker.iife.js`, which
 * `usePagePicker` injects into the *remote* page over `execute_js`. Nothing
 * here ever runs in the studio's own tab.
 *
 * The whole design rests on one property of the existing live view: it is a
 * CDP screencast of the real page, and `interact` mode dispatches real
 * `Input.*` events into it. So an overlay drawn here is already visible to
 * the user, and their mouse already reaches it -- no coordinate mapping, no
 * separate render path, no backend change. The picker draws in the page and
 * the page is what the user is looking at.
 *
 * The return path is the one thing CDP does not give us for free. Upstream
 * the extension answered `chrome.runtime.sendMessage`; here a completed
 * selection is parked on `window.__cpPickResult` and the studio collects it
 * by polling `take()` through a second `execute_js`. `take()` is
 * destructive-read so a result is delivered exactly once.
 */
import { VisualElementPicker, setPickerSink } from './vendor/content/features/picker/VisualElementPicker'
import { ListSelectionStrategy } from './vendor/content/features/picker/strategies/ListSelectionStrategy'
import { DetailSelectionStrategy } from './vendor/content/features/picker/strategies/DetailSelectionStrategy'
import { SingleSelectionStrategy } from './vendor/content/features/picker/strategies/SingleSelectionStrategy'
import { SelectionHighlightManager } from './vendor/content/services/page/SelectionHighlightManager'
import type { ISelectionStrategy } from './vendor/content/features/picker/strategies/ISelectionStrategy'
import {
  HL_COLOR,
  HL_BG,
  HL_GLOW_STRONG,
  DUR_VALIDATE,
  DUR_FADE,
} from './vendor/content/utils/highlight-tokens'
import {
  PICKER_GLOBAL,
  PICKER_VERSION,
  type HighlightField,
  type PickerApi,
  type PickerMode,
  type PickMessage,
} from './protocol'
import { enrich } from './enrich'
import { runPreview, runPreviewRows, runSteps } from './preview'

const TEST_HIGHLIGHT_CLASS = 'crawlpilot-test-highlight'

let picker: VisualElementPicker | null = null
let highlights: SelectionHighlightManager | null = null
let pending: PickMessage | null = null

// Enrich here rather than in the studio: `enrich` answers questions that only
// the live DOM can answer (which element holds a column's value, what the full
// candidate chain for a picked node is), and by the time the payload reaches
// the studio that DOM is a screencast frame. See `enrich.ts`.
setPickerSink((msg) => {
  pending = enrich(msg as PickMessage)
})

function makeStrategy(mode: PickerMode): ISelectionStrategy {
  switch (mode) {
    case 'single':
      return new SingleSelectionStrategy()
    case 'detail':
      return new DetailSelectionStrategy()
    default:
      return new ListSelectionStrategy()
  }
}

function start(mode: PickerMode = 'list', action: 'extract' | 'click' = 'extract') {
  cancel()
  pending = null
  picker = new VisualElementPicker()
  const strategy = makeStrategy(mode)
  strategy.preferredAction = action
  // A cancel (Escape) is reported through the same slot as a selection, so
  // the studio's poll loop has exactly one thing to watch and can always
  // distinguish "user backed out" from "still picking".
  picker.activate(strategy, () => {
    pending = { type: 'PICKER_CANCELLED' }
  })
}

function cancel() {
  if (picker) {
    picker.deactivate()
    picker = null
  }
}

/** ↑ / ↓ / Enter, driven from the studio's buttons. See `usePagePicker`. */
function action(key: 'ArrowUp' | 'ArrowDown' | 'Enter') {
  picker?.handleExternalAction(key)
}

/** Destructive read -- a result is handed to the studio exactly once. */
function take(): PickMessage | null {
  const result = pending
  pending = null
  return result
}

function isPicking(): boolean {
  return picker !== null
}

/**
 * Mark everything already picked, so the page shows its own state.
 *
 * Without this an author picking a twelve-column table has no way to tell
 * which cells they have already taken -- the page looks identical before and
 * after every click. The extension solved it the same way, and it is the
 * difference between picking and guessing.
 */
function showHighlights(elements: HighlightField[]) {
  highlights ??= new SelectionHighlightManager()
  highlights.showHighlights(elements as Parameters<SelectionHighlightManager['showHighlights']>[0])
}

function clearHighlights() {
  highlights?.clearHighlights()
}

/**
 * Flash every match of a selector, labelled `Match N`, then fade.
 *
 * Ported from `ContentScriptController.testSelector` -- it lived in the
 * extension's message router rather than in any of the vendored modules, so
 * it comes across here rather than under `vendor/`. Returns the match count
 * so the studio can report "3 matches" without a second round trip.
 */
function testSelector(selector: string): number {
  document.querySelectorAll('.' + TEST_HIGHLIGHT_CLASS).forEach((el) => el.remove())

  const trimmed = selector.trim()
  if (!trimmed) return 0

  let elements: Element[] = []
  try {
    const isXPath =
      trimmed.startsWith('/') ||
      trimmed.startsWith('(') ||
      trimmed.startsWith('./') ||
      trimmed.startsWith('id(')

    if (isXPath) {
      const result = document.evaluate(trimmed, document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null)
      for (let i = 0; i < result.snapshotLength; i++) {
        const node = result.snapshotItem(i)
        if (node instanceof Element) elements.push(node)
      }
    } else {
      elements = Array.from(document.querySelectorAll(trimmed))
    }
  } catch {
    // An invalid selector is an answer, not a crash: zero matches.
    return 0
  }

  if (elements.length === 0) return 0
  elements[0].scrollIntoView({ behavior: 'auto', block: 'center' })

  elements.forEach((el, index) => {
    const rect = el.getBoundingClientRect()
    const box = document.createElement('div')
    box.className = TEST_HIGHLIGHT_CLASS
    box.style.cssText = `
      position: fixed !important;
      top: ${rect.top}px !important;
      left: ${rect.left}px !important;
      width: ${rect.width}px !important;
      height: ${rect.height}px !important;
      border: 2px solid ${HL_COLOR} !important;
      background-color: ${HL_BG} !important;
      z-index: 2147483647 !important;
      pointer-events: none !important;
      box-shadow: 0 0 12px ${HL_GLOW_STRONG} !important;
      border-radius: 4px !important;
      transition: opacity 0.3s ease !important;
    `
    // Labelling every match on a 500-row table is noise, and the boxes
    // overlap anyway -- upstream's cutoff of 20 is kept.
    if (index < 20) {
      const label = document.createElement('div')
      label.textContent = `Match ${index + 1}`
      label.style.cssText = `
        position: absolute !important;
        top: -24px !important;
        left: 0 !important;
        background: ${HL_COLOR} !important;
        color: #000 !important;
        font-size: 11px !important;
        font-family: sans-serif !important;
        font-weight: bold !important;
        padding: 3px 8px !important;
        border-radius: 4px !important;
        white-space: nowrap !important;
        box-shadow: 0 2px 4px rgba(0,0,0,0.2) !important;
      `
      box.appendChild(label)
    }
    document.body.appendChild(box)
    setTimeout(() => {
      box.style.opacity = '0'
      setTimeout(() => box.remove(), DUR_FADE)
    }, DUR_VALIDATE)
  })

  return elements.length
}

/**
 * The single export, deliberately -- adding any *named* export alongside the
 * default would make rollup emit a namespace object instead of the API.
 * Shared constants and types live in `protocol.ts` for that reason.
 */
const api: PickerApi = {
  version: PICKER_VERSION,
  start,
  cancel,
  action,
  take,
  isPicking,
  showHighlights,
  clearHighlights,
  testSelector,
  preview: (fields) => runPreview(fields as Parameters<typeof runPreview>[0]),
  previewRows: (fields) => runPreviewRows(fields as Parameters<typeof runPreviewRows>[0]),
  applySteps: (steps) => runSteps(steps as Parameters<typeof runSteps>[0]),
}

/**
 * Install explicitly, rather than leaning on the IIFE's `var __cpPicker = ...`
 * binding to become a global.
 *
 * It would not. `execute_js` reaches the page as Playwright's
 * `page.evaluate(script)` (`patchright_driver.py:1412`), which runs the script
 * inside a function scope -- so a top-level `var` is local to that call and
 * vanishes the moment it returns, leaving `window.__cpPicker` undefined and
 * every subsequent call failing. Assigning to `window` here is what survives,
 * and it holds however the caller chooses to wrap the bundle.
 */
;(window as unknown as Record<string, unknown>)[PICKER_GLOBAL] = api

export default api
