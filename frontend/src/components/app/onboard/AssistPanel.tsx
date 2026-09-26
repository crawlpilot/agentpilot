import { useCallback, useMemo, useState } from 'react'
import { TriangleAlert } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { LiveViewPanel } from '@/components/app/LiveViewPanel'
import { usePagePicker } from '@/hooks/usePagePicker'
import { useSessionsList } from '@/hooks/useSessionsList'
import { useAssistHeartbeat, useSubmitAssist } from '@/hooks/useRecipes'
import { useToast } from '@/components/ui/toast'
import { AskRow, type ScopeShape } from './AskRow'
import { detailPickToDraft, scopeFromPick, setReadAttribute, toPreviewFields } from '@/lib/recipe/fromPick'
import {
  EMPTY,
  isAnswered,
  nextUnanswered,
  resolutionFor,
  type AskState,
} from '@/lib/recipe/assistRoute'
import type { Candidate } from '@/lib/recipe/types'
import type { PendingAsk, RecipeResolution } from '@/lib/api/types'

/**
 * The build stopped and is asking.
 *
 * The panel owns three things and delegates the rest: the browser session the
 * build parked on, the conversions a pick needs (`detailPickToDraft`,
 * `scopeFromPick`, the read-back), and **one state object per ask**.
 *
 * That last one is load-bearing. It used to be four separate per-field maps —
 * `answers`, `picks`, `scoped`, `recorded` — and an answer was *derived* from
 * whichever of them happened to be populated. Two bugs came straight out of
 * that shape and between them made the screen unusable:
 *
 * - A route gaining its first entry made the ask look answered, so the row
 *   replaced its own editor with a summary and unmounted the thing being used
 *   to build it. The recorder that got unmounted held a page-wide lock nothing
 *   could then release, which disabled every other ask on the screen.
 * - `clear()` pruned three of the four maps and left the fourth, so Change
 *   re-derived the same answer and the row snapped shut again.
 *
 * One object, and `mode === 'done'` set only by committing, makes both of those
 * unwriteable. See `assistRoute.ts`.
 */
export function AssistPanel({
  recipeId,
  runId,
  asks,
}: {
  recipeId: string
  runId: string
  asks: PendingAsk[]
}) {
  const { toast } = useToast()
  const submit = useSubmitAssist(recipeId, runId)
  // Answering an ask properly is a reload, a few picks and a look at what it
  // read. The park has to be bounded -- it holds a worker slot, a warm
  // identity, a browser and a proxy pin -- so this says somebody is still here,
  // rather than the ceiling being the budget for doing the work.
  useAssistHeartbeat(recipeId, runId, asks.length > 0)

  const [states, setStates] = useState<Record<string, AskState>>({})
  const [selected, setSelected] = useState<string>(asks[0]?.field ?? '')

  // The run's own session is the one showing the stuck page. It is named after
  // the run, which is how it is found among whatever else is open.
  //
  // **No fallback to "whatever else is open."** There used to be one, and it
  // injected the picker into an unrelated session: the buttons stayed enabled,
  // the live view showed a different page, and a pick bound a selector from
  // somewhere else entirely. A missing run session is a state to report, not
  // to substitute for.
  const { data } = useSessionsList()
  const sessions = (data?.sessions ?? []).filter((s) => s.state === 'active')
  const runSession = sessions.find((s) => s.session_id === `recipe-run-${runId}`)
  const [sessionId, setSessionId] = useState<string | null>(null)
  const active = sessionId ?? runSession?.session_id ?? null
  const picker = usePagePicker(active)

  const stateOf = useCallback(
    (field: string): AskState => states[field] ?? EMPTY,
    [states],
  )

  const patch = useCallback((field: string, next: Partial<AskState>) => {
    setStates((prev) => ({ ...prev, [field]: { ...(prev[field] ?? EMPTY), ...next } }))
  }, [])

  /** Commit an ask and move on, which is what makes the list read as a queue. */
  const commit = useCallback(
    (field: string, next: Partial<AskState>) => {
      setStates((prev) => {
        const settled = { ...prev, [field]: { ...(prev[field] ?? EMPTY), ...next, mode: 'done' as const } }
        setSelected((current) => (current === field ? nextUnanswered(asks, settled, field) : current))
        return settled
      })
    },
    [asks],
  )

  const resolutions = useMemo(() => {
    const out: RecipeResolution[] = []
    for (const ask of asks) {
      const one = resolutionFor(ask.field, stateOf(ask.field))
      if (one) out.push(one)
    }
    return out
  }, [asks, stateOf])

  /** What a candidate chain actually reads on the live page, as replay would. */
  const readBack = useCallback(
    async (candidates: Candidate[]): Promise<string> => {
      try {
        const [result] = await picker.preview(
          toPreviewFields([{ name: 'probe', spec: { type: { kind: 'scalar' } }, candidates }]),
        )
        if (!result || result.value === null) return ''
        return Array.isArray(result.value) ? result.value.join(', ') : result.value
      } catch {
        // A failed read-back costs the preview line, never the answer.
        return ''
      }
    },
    [picker],
  )

  function pickFailed(err: unknown) {
    const message = err instanceof Error ? err.message : String(err)
    toast({
      title: 'Could not pick on the page',
      description: /404|no session/i.test(message)
        ? 'The build’s browser session is no longer reachable. Describe where the field is, or skip it.'
        : message,
      variant: 'destructive',
    })
  }

  async function pickValue(field: string) {
    try {
      const payload = await picker.pick('detail')
      if (!payload) return
      const draft = detailPickToDraft(payload)
      if (draft.candidates.length === 0) {
        toast({ title: 'No usable selector for that element' })
        return
      }
      const preview = (await readBack(draft.candidates)) || draft.preview || ''
      // Not committed here: the person may want to change the attribute first,
      // and a pick they have not looked at is not an answer they have given.
      patch(field, { pick: { candidates: draft.candidates, spec: draft.spec, preview } })
    } catch (err) {
      pickFailed(err)
    }
  }

  async function pickScope(field: string, shape: ScopeShape) {
    try {
      const payload = await picker.pick('detail')
      if (!payload) return
      const scope = scopeFromPick(payload)
      if (scope.locators.length === 0) {
        toast({ title: 'No usable selector for that region' })
        return
      }
      const selector = (scope.locators[0] as { selector?: string }).selector ?? ''
      const html = selector ? await picker.outerHtml(selector) : ''
      commit(field, {
        scope: { selector, matched: scope.matched, preview: scope.preview },
        answer: {
          field,
          action: 'scope',
          locators: scope.locators as unknown as Array<Record<string, unknown>>,
          shape,
          html,
        },
      })
    } catch (err) {
      pickFailed(err)
    }
  }

  /** Re-read a pick through a different attribute, in place. */
  async function changeAttribute(field: string, attribute: string) {
    const current = stateOf(field).pick
    if (!current) return
    const candidates = setReadAttribute(current.candidates, attribute)
    const preview = await readBack(candidates)
    patch(field, { pick: { ...current, candidates, preview } })
  }

  function send() {
    submit.mutate(resolutions, {
      onSuccess: (resp) =>
        toast({
          title: 'Sent',
          description: `Carrying on with ${resp.accepted.length} answered.`,
        }),
      onError: (err) =>
        toast({ title: 'Could not send', description: err.message, variant: 'destructive' }),
    })
  }

  const answered = resolutions.length

  return (
    <div className="flex h-[calc(100vh-3rem)] min-h-0 flex-col gap-3 p-4">
      <div className="flex flex-wrap items-center gap-2">
        <Badge variant="warning">waiting for you</Badge>
        <h2 className="text-sm font-medium">
          {asks.length === 1
            ? 'One field it could not settle'
            : `${asks.length} fields it could not settle`}
        </h2>
        <span className="text-xs text-muted-foreground">
          {answered} of {asks.length} answered &mdash; the page below is where it stopped.
        </span>
        <div className="ml-auto flex items-center gap-2">
          {sessions.length > 1 && (
            <Select value={active ?? ''} onValueChange={setSessionId}>
              <SelectTrigger className="h-7 w-52 text-xs">
                <SelectValue placeholder="pick a session" />
              </SelectTrigger>
              <SelectContent>
                {sessions.map((s) => (
                  <SelectItem key={s.session_id} value={s.session_id}>
                    {s.session_id === `recipe-run-${runId}` ? 'this build' : s.name || s.session_id}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          )}
          <Button onClick={send} disabled={answered === 0 || submit.isPending}>
            {submit.isPending ? 'Sending…' : `Continue with ${answered} answered`}
          </Button>
        </div>
      </div>

      {picker.error && (
        // Set by the hook and, until it was rendered here, shown nowhere -- so
        // an injection that failed left the panel looking idle.
        <p className="flex items-center gap-1.5 rounded bg-destructive/10 px-2 py-1 text-[11px] text-destructive">
          <TriangleAlert className="size-3.5 shrink-0" />
          {picker.error}
        </p>
      )}

      <div className="grid min-h-0 flex-1 grid-cols-[minmax(0,1fr)_26rem] gap-3">
        <div className="min-h-0 overflow-hidden rounded-md border border-border">
          {active ? (
            <LiveViewPanel sessionId={active} />
          ) : (
            <div className="flex h-full items-center justify-center p-6 text-center text-sm text-muted-foreground">
              The build&rsquo;s browser session is not available any more, so you cannot pick on the
              page. You can still describe where a field is, or skip it.
            </div>
          )}
        </div>

        <div className="flex min-h-0 flex-col gap-1.5 overflow-y-auto">
          {asks.map((ask) => (
            <AskRow
              key={ask.field}
              ask={ask}
              state={stateOf(ask.field)}
              selected={selected === ask.field}
              canPick={active !== null}
              picker={picker}
              onSelect={() => setSelected(ask.field)}
              onState={(next) => patch(ask.field, next)}
              onDone={(next) => commit(ask.field, next)}
              onPickValue={() => void pickValue(ask.field)}
              onPickScope={(shape) => void pickScope(ask.field, shape)}
              onAttribute={(attribute) => void changeAttribute(ask.field, attribute)}
            />
          ))}
          {asks.every((a) => isAnswered(states[a.field])) && asks.length > 0 && (
            <p className="rounded bg-success/10 px-2 py-1 text-[11px] text-muted-foreground">
              All {asks.length} answered. Press Continue to carry on with the build.
            </p>
          )}
        </div>
      </div>
    </div>
  )
}
