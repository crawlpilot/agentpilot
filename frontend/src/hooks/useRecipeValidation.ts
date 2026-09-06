import { useCallback, useRef, useState } from 'react'
import { executeSession } from '@/lib/api/sessions'
import { useAuth } from '@/lib/auth/AuthContext'
import type { UsePagePicker } from '@/hooks/usePagePicker'
import type { PreviewResult, PreviewRowsResult, StepOutcome } from '@/lib/picker/preview'
import type { ValidationGroup } from '@/lib/recipe/validatePlan'

export interface GroupValidation {
  groupId: string
  steps: StepOutcome[]
  fields: PreviewResult[]
  rowFields: PreviewRowsResult[]
  /** Set when the group could not be reached at all. */
  error?: string
}

export interface ValidationRun {
  url: string
  groups: GroupValidation[]
}

export interface UseRecipeValidation {
  running: boolean
  /** Which group is executing, for progress. */
  progress: string | null
  result: ValidationRun | null
  error: string | null
  validate: (url: string, plan: ValidationGroup[]) => Promise<void>
  clear: () => void
}

/**
 * Run a recipe the way a real replay would: fresh page, steps, then reads.
 *
 * Deliberately **per group, each starting with its own navigation**, mirroring
 * `replay.py::_replay_group`. That is the expensive choice and the correct
 * one -- it is what makes `global_setup` re-run for every group, and what
 * proves a group's steps are self-sufficient from a clean load rather than
 * quietly depending on state an earlier group left behind.
 *
 * The most useful thing it catches is the one an author cannot see: a field
 * that previews fine because they opened the drawer by hand while picking, and
 * whose reveal step was never recorded. On a fresh page that field is empty,
 * which is exactly what would happen at 3am on a schedule.
 */
export function useRecipeValidation(
  sessionId: string | null,
  picker: UsePagePicker,
): UseRecipeValidation {
  const { apiKey } = useAuth()
  const [running, setRunning] = useState(false)
  const [progress, setProgress] = useState<string | null>(null)
  const [result, setResult] = useState<ValidationRun | null>(null)
  const [error, setError] = useState<string | null>(null)
  const active = useRef(false)

  const clear = useCallback(() => {
    setResult(null)
    setError(null)
  }, [])

  const validate = useCallback(
    async (url: string, plan: ValidationGroup[]) => {
      if (!sessionId || !apiKey) {
        setError('No active session')
        return
      }
      setRunning(true)
      setError(null)
      setResult(null)
      active.current = true

      const groups: GroupValidation[] = []
      try {
        for (const group of plan) {
          if (!active.current) return
          setProgress(group.groupId)

          try {
            // A real navigation, not a soft reset: the point is to prove the
            // recipe works on a page it has not already been clicked on.
            await executeSession(apiKey, sessionId, {
              actions: [
                { type: 'navigate', url },
                // The picker's own reads are synchronous against whatever the
                // DOM holds, so waiting for the load to settle here is what
                // stops a fast machine reporting a half-rendered page as a
                // broken recipe.
                { type: 'wait', ms: 1200 },
              ],
            })
          } catch (err) {
            groups.push({
              groupId: group.groupId,
              steps: [],
              fields: [],
              rowFields: [],
              error: err instanceof Error ? err.message : 'navigation failed',
            })
            continue
          }

          // The navigation replaced the document, so the injected picker is
          // gone with it. `usePagePicker` re-installs on the next call via its
          // version guard -- nothing to do here but call it.
          const steps = group.steps.length > 0 ? await picker.applySteps(group.steps) : []
          const [fields, rowFields] = await Promise.all([
            group.fields.length > 0 ? picker.preview(group.fields) : Promise.resolve([]),
            group.rowFields.length > 0 ? picker.previewRows(group.rowFields) : Promise.resolve([]),
          ])
          groups.push({ groupId: group.groupId, steps, fields, rowFields })
        }
        setResult({ url, groups })
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Validation failed')
      } finally {
        active.current = false
        setRunning(false)
        setProgress(null)
      }
    },
    [apiKey, picker, sessionId],
  )

  return { running, progress, result, error, validate, clear }
}
