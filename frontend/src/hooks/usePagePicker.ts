import { useCallback, useEffect, useRef, useState } from 'react'
import { executeSession } from '@/lib/api/sessions'
import { useAuth } from '@/lib/auth/AuthContext'
import pickerBundle from '@/lib/picker/generated/picker.iife.js?raw'
import type { PreviewField, PreviewResult, PreviewStep, StepOutcome } from '@/lib/picker/preview'
import {
  PICKER_GLOBAL,
  PICKER_VERSION,
  type PickerMode,
  type PickMessage,
  type PickPayload,
} from '@/lib/picker/protocol'

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
  /** Start picking; resolves with the payload, or null if cancelled. */
  pick: (mode: PickerMode) => Promise<PickPayload | null>
  /** Stop an in-flight pick. */
  cancel: () => void
  /** ↑ / ↓ / Enter, from the panel's own buttons. */
  refine: (key: 'ArrowUp' | 'ArrowDown' | 'Enter') => void
  /** Flash a selector's matches in the page; resolves with the match count. */
  testSelector: (selector: string) => Promise<number>
  /** Resolve fields against the live page, exactly as replay would. */
  preview: (fields: PreviewField[]) => Promise<PreviewResult[]>
  /** Apply reveal steps in the page before previewing. A rehearsal, not replay. */
  applySteps: (steps: PreviewStep[]) => Promise<StepOutcome[]>
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
    (key: 'ArrowUp' | 'ArrowDown' | 'Enter') => {
      // Driven from buttons rather than keystrokes on purpose. The live view
      // forwards keys from `window` only when nothing editable has focus, and
      // during a pick the studio's own panel usually does -- so the arrow keys
      // the picker listens for would never leave the browser. The extension
      // hit the same wall and added an explicit action message for it.
      void run(`window.${PICKER_GLOBAL}.action(${JSON.stringify(key)})`).catch(() => {})
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

  const applySteps = useCallback(
    async (steps: PreviewStep[]): Promise<StepOutcome[]> => {
      // `applySteps` is async in the page, and `page.evaluate` awaits a
      // returned promise -- so the outcomes come back resolved.
      const out = await run(`window.${PICKER_GLOBAL}.applySteps(${JSON.stringify(steps)})`)
      return Array.isArray(out) ? (out as StepOutcome[]) : []
    },
    [run],
  )

  const pick = useCallback(
    async (mode: PickerMode): Promise<PickPayload | null> => {
      setError(null)
      setStatus('installing')
      active.current = true

      try {
        const action = mode === 'single' ? 'click' : 'extract'
        await run(`window.${PICKER_GLOBAL}.start(${JSON.stringify(mode)}, ${JSON.stringify(action)})`)
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

  return { status, error, pick, cancel, refine, testSelector, preview, applySteps }
}
