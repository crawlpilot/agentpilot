import { useCallback, useEffect, useRef, useState } from 'react'
import { Circle, PlayCircle, Square, Trash2, TriangleAlert } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { useToast } from '@/components/ui/toast'
import type { PreviewStep, StepOutcome } from '@/lib/picker/preview'
import type { usePagePicker } from '@/hooks/usePagePicker'

/**
 * Record the route to a field the model could not find on its own.
 *
 * Pointing at an element answers *where is this?*. Some fields need the other
 * question — *how do I get to it?* — and no selector describes three clicks, a
 * scroll and a dismissal. This records that, and the recipe replays it.
 *
 * Two things about the UI follow from what recording is:
 *
 * - **The page must respond normally.** Unlike picking, nothing is swallowed
 *   and no overlay is drawn, so the person just works the page in the live view
 *   as they would anywhere. The panel polls for what has arrived so they can
 *   see it accumulating rather than trusting that it is.
 * - **"Try these" exists because a recording is a claim.** It replays the steps
 *   in the page, which is a rehearsal rather than replay (`preview.ts::runSteps`
 *   dispatches from page script, not the trusted CDP click) — so a step that
 *   fails here is a genuine problem worth seeing before it is submitted, even
 *   though one that passes is not yet proof.
 */

/** Slow enough not to flood the session, fast enough to feel live. */
const POLL_MS = 700

export function StepRecorder({
  picker,
  field,
  disabled,
  steps,
  onChange,
  recordingField,
  onRecordingChange,
}: {
  picker: ReturnType<typeof usePagePicker>
  field: string
  disabled: boolean
  steps: PreviewStep[]
  onChange: (field: string, steps: PreviewStep[]) => void
  /**
   * Which field owns the in-page recorder right now, panel-wide.
   *
   * There is ONE `Recorder` in the page (`entry.ts` constructs it once) and
   * `start()` clears its buffer, so a second row starting a recording wipes the
   * first row's steps and then feeds its own into the first row's poller.
   * Ownership has to be decided above the row.
   */
  recordingField: string | null
  onRecordingChange: (field: string | null) => void
}) {
  const { toast } = useToast()
  const recording = recordingField === field
  const [outcomes, setOutcomes] = useState<StepOutcome[] | null>(null)
  const [trying, setTrying] = useState(false)
  // Read by the interval, which must see a stop that happened after it started.
  const live = useRef(false)
  // The effect must not depend on either of these. `picker` is a fresh object
  // from `usePagePicker` on every render, so depending on it tore the interval
  // down and rebuilt it on every parent render -- and the parent re-renders on
  // every sessions poll and every assist heartbeat, which stalled the live
  // step list indefinitely.
  const pickerRef = useRef(picker)
  pickerRef.current = picker
  const onChangeRef = useRef(onChange)
  onChangeRef.current = onChange

  useEffect(() => {
    if (!recording) return
    live.current = true
    const timer = setInterval(() => {
      if (!live.current) return
      void pickerRef.current
        .takeRecording()
        .then((got) => {
          if (live.current && got.length) onChangeRef.current(field, got)
        })
        .catch(() => {
          // A dropped poll is not worth a toast; `stop()` reports for real.
        })
    }, POLL_MS)
    return () => {
      live.current = false
      clearInterval(timer)
    }
  }, [recording, field])

  const start = useCallback(async () => {
    setOutcomes(null)
    onChange(field, [])
    try {
      await picker.startRecording()
    } catch (err) {
      // Same silence the picker had: `startRecording` rejects when the
      // injection fails (an expired session 404s), and without this the button
      // simply did nothing.
      toast({
        title: 'Could not start recording',
        description: err instanceof Error ? err.message : String(err),
        variant: 'destructive',
      })
      return
    }
    onRecordingChange(field)
  }, [picker, field, onChange, onRecordingChange, toast])

  const stop = useCallback(async () => {
    live.current = false
    onRecordingChange(null)
    try {
      onChange(field, await picker.stopRecording())
    } catch (err) {
      toast({
        title: 'Could not read the recording back',
        description: err instanceof Error ? err.message : String(err),
        variant: 'destructive',
      })
    }
  }, [picker, field, onChange, onRecordingChange, toast])

  async function tryThese() {
    setTrying(true)
    try {
      // Only what the server will actually keep -- see `keepable`.
      setOutcomes(await picker.applySteps(steps.filter(keepable)))
    } catch (err) {
      toast({
        title: 'Could not replay the steps',
        description: err instanceof Error ? err.message : String(err),
        variant: 'destructive',
      })
    } finally {
      setTrying(false)
    }
  }

  const dropped = steps.filter((s) => !keepable(s)).length
  const busyElsewhere = recordingField !== null && !recording

  return (
    <div className="flex flex-col gap-1.5 rounded-md border border-dashed border-border p-2">
      <div className="flex items-center gap-1.5">
        <Circle
          className={`size-3.5 shrink-0 ${
            recording ? 'animate-pulse fill-destructive text-destructive' : 'text-muted-foreground'
          }`}
        />
        <span className="text-[11px] font-medium">…or show it how to get there</span>
      </div>
      <p className="text-[11px] text-muted-foreground">
        {recording
          ? 'Recording — work the page in the view on the left. Clicks, scrolls and typing are kept; everything else is ignored.'
          : 'Record the clicks that reveal the field. The recipe replays them on every run, so it works when nobody is watching.'}
      </p>

      <div className="flex flex-wrap items-center gap-1">
        {recording ? (
          <Button size="sm" variant="destructive" className="h-6 px-2 text-[11px]" onClick={() => void stop()}>
            <Square className="size-3" />
            Stop
          </Button>
        ) : (
          <Button
            size="sm"
            variant="outline"
            className="h-6 px-2 text-[11px]"
            disabled={disabled || busyElsewhere}
            title={busyElsewhere ? `Stop the recording on ${recordingField} first` : undefined}
            onClick={() => void start()}
          >
            <Circle className="size-3" />
            {steps.length ? 'Record again' : 'Record'}
          </Button>
        )}
        {steps.length > 0 && !recording && (
          <Button
            size="sm"
            variant="outline"
            className="h-6 px-2 text-[11px]"
            disabled={disabled || trying}
            onClick={() => void tryThese()}
            title="Replay them in the page now, so a step that does not work is visible before you send it"
          >
            <PlayCircle className="size-3" />
            {trying ? 'Trying…' : 'Try these'}
          </Button>
        )}
      </div>

      {dropped > 0 && (
        // The rehearsal used to run the whole client-side list while the server
        // silently discarded some of it (`parse_recorded_steps` drops unknown
        // ops and undispatchable targets), so "Try these" could pass on a route
        // that would be stored with holes in it.
        <p className="flex items-start gap-1.5 rounded bg-warning/10 px-2 py-1 text-[11px] text-muted-foreground">
          <TriangleAlert className="mt-0.5 size-3 shrink-0 text-warning" />
          {dropped === 1 ? 'One action was' : `${dropped} actions were`} not recordable &mdash; no
          stable selector for what was clicked. They are shown struck through and will not be
          saved.
        </p>
      )}

      {steps.length > 0 && (
        <ol className="flex flex-col gap-0.5">
          {steps.map((step, i) => {
            const kept = keepable(step)
            return (
              <li key={i} className="flex items-center gap-1.5 text-[11px]">
                <span className="w-4 shrink-0 text-right text-muted-foreground">{i + 1}</span>
                <Badge
                  variant="outline"
                  className={`shrink-0 px-1 py-0 font-mono text-[10px] ${kept ? '' : 'opacity-50'}`}
                >
                  {step.op}
                </Badge>
                <span
                  className={`min-w-0 flex-1 truncate text-muted-foreground ${
                    kept ? '' : 'line-through opacity-60'
                  }`}
                >
                  {step.text || step.selector || '—'}
                </span>
                {outcomes?.[i] && (
                  <Badge
                    variant={
                      outcomes[i].status === 'ok'
                        ? 'success'
                        : outcomes[i].status === 'failed'
                          ? 'destructive'
                          : 'warning'
                    }
                    className="shrink-0 px-1 py-0 text-[10px]"
                  >
                    {outcomes[i].status}
                  </Badge>
                )}
                {!recording && (
                  <Button
                    size="sm"
                    variant="ghost"
                    className="size-5 shrink-0 p-0"
                    title="Drop this step"
                    onClick={() => onChange(field, steps.filter((_, j) => j !== i))}
                  >
                    <Trash2 className="size-3" />
                  </Button>
                )}
              </li>
            )
          })}
        </ol>
      )}

      {outcomes && outcomes.some((o) => o.status === 'failed') && (
        <p className="rounded bg-warning/10 px-2 py-1 text-[11px] text-muted-foreground">
          A step failed when replayed. It may depend on something your session already had
          open — reload the page and record it again from the top.
        </p>
      )}
    </div>
  )
}

/**
 * Ops the server will keep, mirroring `assist.py::_RECORDABLE_OPS`.
 *
 * A step the recorder could not give a stable CSS selector arrives with none
 * (`record.ts` marks it rather than dropping it silently, so the person can see
 * that their click was not captured). Everything else needs a target except
 * `press` and a page-level `scroll`, which is the same rule
 * `parse_recorded_steps` applies.
 */
const RECORDABLE_OPS = new Set([
  'click',
  'fill',
  'select_option',
  'press',
  'scroll',
  'scroll_into_view',
  'hover',
])

function keepable(step: PreviewStep): boolean {
  if (!RECORDABLE_OPS.has(step.op)) return false
  if (step.selector) return true
  return step.op === 'press' || step.op === 'scroll'
}
