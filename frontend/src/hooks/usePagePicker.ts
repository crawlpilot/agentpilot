import { useCallback, useEffect, useRef, useState } from 'react'
import { executeSession } from '@/lib/api/sessions'
import { useAuth } from '@/lib/auth/AuthContext'
import pickerBundle from '@/lib/picker/generated/picker.iife.js?raw'
import type {
  PreviewField,
  PreviewResult,
  PreviewRowsField,
  PreviewRowsResult,
  PreviewStep,
  StepOutcome,
} from '@/lib/picker/preview'
import {
  PICKER_GLOBAL,
  PICKER_VERSION,
  type HighlightField,
  type PickerMode,
  type PickMessage,
  type PickPayload,
} from '@/lib/picker/protocol'

// A region is small by construction -- that is what scoping buys -- so this
// only bites on someone selecting most of the page, where the scope was worth
// little anyway. Mirrors `_MAX_FRAGMENT_CHARS` in `selector_agent.py`.
const MAX_FRAGMENT_CHARS = 20_000

// Elements that are never the value a person pointed at, and are routinely most
// of the bytes. Mirrors `_DEAD_MARKUP` in `selector_agent.py`.
const DEAD_TAGS = ['script', 'style', 'svg', 'noscript', 'template', 'iframe', 'canvas']

// Attributes worth keeping: the ones a selector can be built out of, plus the
// few that carry a value. Everything else on a React page is generated, and
// `propose_within` is explicitly told it rarely needs a class name inside a
// region that is already scoped. Mirrors `_KEEP_ATTR` in `selector_agent.py`.
const KEEP_ATTR = '^(?:id|class|role|itemprop|itemtype|href|src|alt|title|value|content|type|name|colspan|rowspan)$|^(?:data|aria)-'

/**
 * An expression that returns the region's markup with the dead parts removed.
 *
 * Built as a string because it runs in the page through `execute_js`, not here.
 * The selector and the constants travel as JSON literals so nothing in them is
 * ever read as syntax.
 */
export const PRUNED_OUTER_HTML = (selector: string): string =>
  `(function(){` +
  `var e=document.querySelector(${JSON.stringify(selector)});` +
  `if(!e)return '';` +
  `var c=e.cloneNode(true);` +
  `var dead=${JSON.stringify(DEAD_TAGS)};` +
  `var keep=new RegExp(${JSON.stringify(KEEP_ATTR)},'i');` +
  `var strip=function(n){` +
  `for(var i=n.attributes.length-1;i>=0;i--){` +
  `var a=n.attributes[i].name;if(!keep.test(a))n.removeAttribute(a);}};` +
  `strip(c);` +
  `for(var d=0;d<dead.length;d++){` +
  `var gone=c.querySelectorAll(dead[d]);` +
  `for(var g=0;g<gone.length;g++){if(gone[g].parentNode)gone[g].parentNode.removeChild(gone[g]);}}` +
  `var all=c.querySelectorAll('*');` +
  `for(var j=0;j<all.length;j++){strip(all[j]);}` +
  // Comments are hydration markers on a React page and say nothing about where
  // a value is. A TreeWalker is the only way to reach them.
  `var w=document.createTreeWalker(c,NodeFilter.SHOW_COMMENT,null);` +
  `var cs=[];while(w.nextNode()){cs.push(w.currentNode);}` +
  `for(var k=0;k<cs.length;k++){if(cs[k].parentNode)cs[k].parentNode.removeChild(cs[k]);}` +
  `return c.outerHTML.replace(/[ \\t\\r\\n]+/g,' ').slice(0,${MAX_FRAGMENT_CHARS});` +
  `})()`

/**
 * Drives the visual picker inside a live session's page.
 *
 * There is no new transport here and no backend change. The picker is injected
 * with the ordinary `execute_js` action -- the same one the studio's JSON probe
 * already rides on -- and it draws its overlay *in the page*, so the existing
 * CDP screencast renders it and `interact` mode's forwarded mouse events reach
 * it as real input. The user is already looking at, and clicking on, the thing
 * the picker is decorating.
 *
 * The one thing CDP does not hand back for free is the result. `execute_js` is
 * request/response, and a selection happens whenever the user gets round to
 * clicking, so the page parks the finished payload on a global and this polls a
 * destructive `take()` until it appears. A push channel would be nicer -- see
 * the note on `POLL_MS` -- but it needs a backend change and this does not.
 */

/**
 * Poll interval while a pick is in flight.
 *
 * 300ms is a deliberate compromise, not a tuned number. It is well under the
 * time it takes a person to notice they have clicked, and slow enough that a
 * long picking session is a handful of requests per second rather than a
 * flood. It only costs latency *after* the click -- the hover highlight is
 * drawn in the page and arrives on the screencast, so nothing about the
 * picking experience itself waits on this.
 *
 * If it ever needs to be instant, the fix is a CDP `Runtime.addBinding` push
 * over the live-view WebSocket rather than a shorter interval.
 */
const POLL_MS = 300

/** Give up on a pick nobody completes, so a stale poll cannot run forever. */
const POLL_TIMEOUT_MS = 5 * 60 * 1000

export type PickerStatus = 'idle' | 'installing' | 'picking'

/**
 * Everything about a recording, read in one round trip.
 *
 * Four separate `execute_js` calls would be four HTTP requests every 700ms for
 * as long as somebody is recording, to answer four questions about the same
 * object. The composite expression costs one.
 */
export interface RecordingState {
  steps: PreviewStep[]
  /** A pick is in flight; the page is not being observed right now. */
  paused: boolean
  /**
   * The page changed under the recording, so every step after that point
   * targets a different document. The route has to be made again.
   */
  navigated: boolean
  /** The step cap was reached and events are being discarded. */
  full: boolean
}

/** One expression, so a poll is one request. Property order is evaluation order. */
const RECORDING_STATE = (call: string) =>
  `(function(){var p=window.${PICKER_GLOBAL};return {` +
  `steps:p.${call},` +
  `paused:p.isRecordingPaused(),` +
  `navigated:p.didNavigate(),` +
  `full:p.recordingFull()};})()`

function asRecordingState(out: unknown): RecordingState {
  const o = (out ?? {}) as Partial<Record<keyof RecordingState, unknown>>
  return {
    steps: Array.isArray(o.steps) ? (o.steps as PreviewStep[]) : [],
    paused: o.paused === true,
    navigated: o.navigated === true,
    full: o.full === true,
  }
}

/**
 * Install if absent or outdated, then evaluate `expression`.
 *
 * Sent as one script so a pick costs one round trip rather than two, and so
 * there is no window in which a navigation lands between the check and the
 * call. The version guard makes it idempotent: the bundle is ~77KB, and
 * re-sending it on every hover would be wasteful, but re-sending it after a
 * navigation is mandatory -- a new document has no `window.__cpPicker` at all.
 *
 * Wrapped in a function so it is a single expression: `execute_js` reaches the
 * page as Playwright's `page.evaluate`, and the bundle's own top-level `var`
 * would not survive that scope (which is why `entry.ts` assigns to `window`).
 */
function script(expression: string): string {
  return `(function () {
  if (!window.${PICKER_GLOBAL} || window.${PICKER_GLOBAL}.version !== ${PICKER_VERSION}) {
    ${pickerBundle}
  }
  return ${expression};
})()`
}

export interface UsePagePicker {
  status: PickerStatus
  error: string | null
  /**
   * Start picking; resolves with the payload, or null if cancelled.
   *
   * `action` is the strategy's *preference*, not what the picker does: an
   * `extract` pick reads the element, a `click` pick is choosing a control to
   * act on later. It reaches `ISelectionStrategy.preferredAction`.
   */
  pick: (mode: PickerMode, action?: 'extract' | 'click') => Promise<PickPayload | null>
  /** Stop an in-flight pick. */
  cancel: () => void
  /** ↑ / ↓ / Enter / Unpin, from the panel's own buttons. */
  refine: (key: 'ArrowUp' | 'ArrowDown' | 'Enter' | 'Unpin') => void
  /** What the selection is on right now, for showing it back to the user. */
  selection: () => Promise<{ tag: string; text: string; pinned: boolean } | null>
  /** Watch what the user does to the page; resolves with the recorded steps. */
  startRecording: (seed?: PreviewStep[]) => Promise<void>
  stopRecording: () => Promise<RecordingState>
  /** Where the recording is up to, without ending it. */
  pollRecording: () => Promise<RecordingState>
  /**
   * Pick an element without ending the recording, folding it in as a `select`.
   *
   * Fire-and-forget, unlike `pick`: the result goes into the route rather than
   * coming back here, and the panel sees it arrive through `pollRecording`.
   */
  pickInRecording: (action: 'extract' | 'click') => Promise<void>
  /**
   * Stop and restart observing, without ending the recording.
   *
   * Not the same pause a pick takes. This one is for getting to the thing
   * worth recording — the clicks it takes to find a field are not the route to
   * it, and without this they all land in the list to be deleted afterwards.
   */
  pauseRecording: () => Promise<void>
  resumeRecording: () => Promise<void>
  /** Flash a selector's matches in the page; resolves with the match count. */
  testSelector: (selector: string) => Promise<number>
  /** A region's markup, for handing the model something to search inside. */
  outerHtml: (selector: string) => Promise<string>
  /** Resolve fields against the live page, exactly as replay would. */
  preview: (fields: PreviewField[]) => Promise<PreviewResult[]>
  /** Read a `dom_rows` table row-wise, exactly as replay would. */
  previewRows: (fields: PreviewRowsField[]) => Promise<PreviewRowsResult[]>
  /** Apply reveal steps in the page before previewing. A rehearsal, not replay. */
  applySteps: (steps: PreviewStep[]) => Promise<StepOutcome[]>
  /** Mark everything already picked, persistently, on the page. */
  showHighlights: (fields: HighlightField[]) => void
}

export function usePagePicker(sessionId: string | null): UsePagePicker {
  const { apiKey } = useAuth()
  const [status, setStatus] = useState<PickerStatus>('idle')
  const [error, setError] = useState<string | null>(null)
  // Survives re-renders and is read by the poll loop, which must be able to
  // see a cancellation that happened after it started.
  const active = useRef(false)

  useEffect(() => {
    return () => {
      active.current = false
    }
  }, [])

  const run = useCallback(
    async (expression: string): Promise<unknown> => {
      if (!sessionId || !apiKey) throw new Error('No active session')
      const result = await executeSession(apiKey, sessionId, {
        actions: [{ type: 'execute_js', script: script(expression) }],
      })
      return result.js_returns?.[0] ?? null
    },
    [apiKey, sessionId],
  )

  const cancel = useCallback(() => {
    active.current = false
    setStatus('idle')
    // Best-effort: if the page has gone the overlay went with it.
    void run(`window.${PICKER_GLOBAL}.cancel()`).catch(() => {})
  }, [run])

  const refine = useCallback(
    (key: 'ArrowUp' | 'ArrowDown' | 'Enter' | 'Unpin') => {
      // Driven from buttons rather than keystrokes on purpose. The live view
      // forwards keys from `window` only when nothing editable has focus, and
      // during a pick the studio's own panel usually does -- so the arrow keys
      // the picker listens for would never leave the browser. The extension
      // hit the same wall and added an explicit action message for it.
      //
      // That indirection is also what made expansion unusable until the picker
      // learned to pin: reaching this button means dragging the cursor across
      // the live view, and every pixel of that was a forwarded `mousemove` that
      // reset the selection back to whatever was underneath.
      void run(`window.${PICKER_GLOBAL}.action(${JSON.stringify(key)})`).catch(() => {})
    },
    [run],
  )

  const selection = useCallback(async () => {
    try {
      const got = await run(`window.${PICKER_GLOBAL}.selection()`)
      return (got as { tag: string; text: string; pinned: boolean } | null) ?? null
    } catch {
      return null
    }
  }, [run])

  const startRecording = useCallback(
    async (seed: PreviewStep[] = []) => {
      // Sent whole rather than resumed in the page, because the panel's copy
      // is the edited one -- rows deleted, relabelled and reordered since the
      // recorder last saw them.
      await run(`window.${PICKER_GLOBAL}.startRecording(${JSON.stringify(seed)})`)
    },
    [run],
  )

  const stopRecording = useCallback(async (): Promise<RecordingState> => {
    return asRecordingState(await run(RECORDING_STATE('stopRecording()')))
  }, [run])

  const pollRecording = useCallback(async (): Promise<RecordingState> => {
    // Polled while recording so the panel can show the steps arriving. A
    // navigation mid-recording tears the page down and the listeners with it,
    // so a throw here is expected rather than exceptional.
    try {
      return asRecordingState(await run(RECORDING_STATE('takeRecording()')))
    } catch {
      return { steps: [], paused: false, navigated: false, full: false }
    }
  }, [run])

  const pauseRecording = useCallback(async () => {
    await run(`window.${PICKER_GLOBAL}.pauseRecording()`)
  }, [run])

  const resumeRecording = useCallback(async () => {
    await run(`window.${PICKER_GLOBAL}.resumeRecording()`)
  }, [run])

  const pickInRecording = useCallback(
    async (action: 'extract' | 'click') => {
      // No poll loop here, unlike `pick`. The result is pushed into the route
      // in the page, and `pollRecording` is already watching for it -- racing
      // a second collector against that one would be two ways for the same
      // selection to arrive.
      await run(`window.${PICKER_GLOBAL}.pickInRecording(${JSON.stringify(action)})`)
    },
    [run],
  )

  const testSelector = useCallback(
    async (selector: string): Promise<number> => {
      const count = await run(`window.${PICKER_GLOBAL}.testSelector(${JSON.stringify(selector)})`)
      return typeof count === 'number' ? count : 0
    },
    [run],
  )

  const outerHtml = useCallback(
    async (selector: string): Promise<string> => {
      // Read straight from the page rather than adding a field to the picker
      // payload, which would mean regenerating the vendored bundle for one
      // string. The selector travels as a JSON literal, so one containing a
      // quote is data rather than syntax -- the discipline `evaluate.py` uses.
      //
      // Pruned IN the page, before the slice. A specifications section on a
      // React page is mostly inline styles, generated class names and inert
      // script tags, so a raw slice of `outerHTML` routinely cut off mid-table:
      // the person pointed at the right region and the model was shown the
      // first third of it. Doing it here rather than server-side is what the
      // real DOM buys -- `prune_fragment` in `selector_agent.py` is the
      // regex-based guard for markup arriving from anywhere else.
      const html = await run(PRUNED_OUTER_HTML(selector))
      return typeof html === 'string' ? html : ''
    },
    [run],
  )

  const preview = useCallback(
    async (fields: PreviewField[]): Promise<PreviewResult[]> => {
      // The fields travel as a JSON literal inside the expression rather than
      // being interpolated as source, so a selector containing a quote is data
      // rather than syntax -- the same discipline `evaluate.py` uses.
      const out = await run(`window.${PICKER_GLOBAL}.preview(${JSON.stringify(fields)})`)
      return Array.isArray(out) ? (out as PreviewResult[]) : []
    },
    [run],
  )

  const previewRows = useCallback(
    async (fields: PreviewRowsField[]): Promise<PreviewRowsResult[]> => {
      const out = await run(`window.${PICKER_GLOBAL}.previewRows(${JSON.stringify(fields)})`)
      return Array.isArray(out) ? (out as PreviewRowsResult[]) : []
    },
    [run],
  )

  const applySteps = useCallback(
    async (steps: PreviewStep[]): Promise<StepOutcome[]> => {
      // `applySteps` is async in the page, and `page.evaluate` awaits a
      // returned promise -- so the outcomes come back resolved.
      const out = await run(`window.${PICKER_GLOBAL}.applySteps(${JSON.stringify(steps)})`)
      return Array.isArray(out) ? (out as StepOutcome[]) : []
    },
    [run],
  )

  const showHighlights = useCallback(
    (fields: HighlightField[]) => {
      // Fire-and-forget: markers are an aid, and a page that navigated away
      // mid-update should not surface an error for losing its decorations.
      const call = fields.length
        ? `window.${PICKER_GLOBAL}.showHighlights(${JSON.stringify(fields)})`
        : `window.${PICKER_GLOBAL}.clearHighlights()`
      void run(call).catch(() => {})
    },
    [run],
  )

  const pick = useCallback(
    async (mode: PickerMode, action?: 'extract' | 'click'): Promise<PickPayload | null> => {
      setError(null)
      setStatus('installing')
      active.current = true

      try {
        const preferred = action ?? (mode === 'single' ? 'click' : 'extract')
        await run(
          `window.${PICKER_GLOBAL}.start(${JSON.stringify(mode)}, ${JSON.stringify(preferred)})`,
        )
      } catch (err) {
        active.current = false
        setStatus('idle')
        setError(err instanceof Error ? err.message : 'Could not start the picker')
        throw err
      }

      setStatus('picking')
      const deadline = Date.now() + POLL_TIMEOUT_MS

      try {
        while (active.current) {
          await new Promise((resolve) => setTimeout(resolve, POLL_MS))
          if (!active.current) return null

          if (Date.now() > deadline) {
            setError('Picking timed out')
            void run(`window.${PICKER_GLOBAL}.cancel()`).catch(() => {})
            return null
          }

          let message: PickMessage | null = null
          try {
            message = (await run(`window.${PICKER_GLOBAL}.take()`)) as PickMessage | null
          } catch {
            // A navigation mid-pick tears down the page and its picker. The
            // next iteration re-injects (the version guard sees no global),
            // so a transient failure here is not worth surfacing -- only a
            // cancellation or a real timeout ends the loop.
            continue
          }

          if (!message) continue
          if (message.type === 'PICKER_CANCELLED') return null
          if (message.type === 'ELEMENT_SELECTED') return message.payload
        }
        return null
      } finally {
        active.current = false
        setStatus('idle')
      }
    },
    [run],
  )

  return {
    status, error, pick, cancel, refine, selection, testSelector, outerHtml,
    preview, previewRows, applySteps, showHighlights,
    startRecording, stopRecording, pollRecording, pickInRecording,
    pauseRecording, resumeRecording,
  }
}
