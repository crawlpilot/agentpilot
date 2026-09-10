import { useEffect, useRef, useState } from 'react'
import { Circle, PlayCircle, Square, Trash2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
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
  disabled,
  steps,
  onChange,
}: {
  picker: ReturnType<typeof usePagePicker>
  disabled: boolean
  steps: PreviewStep[]
  onChange: (steps: PreviewStep[]) => void
}) {
  const [recording, setRecording] = useState(false)
  const [outcomes, setOutcomes] = useState<StepOutcome[] | null>(null)
  const [trying, setTrying] = useState(false)
  // Read by the interval, which must see a stop that happened after it started.
  const live = useRef(false)

  useEffect(() => {
    if (!recording) return
    live.current = true
    const timer = setInterval(() => {
      if (!live.current) return
      void picker.takeRecording().then((got) => {
        if (live.current && got.length) onChange(got)
      })
    }, POLL_MS)
    return () => {
      live.current = false
      clearInterval(timer)
    }
  }, [recording, picker, onChange])

  async function start() {
    setOutcomes(null)
    onChange([])
    await picker.startRecording()
    setRecording(true)
  }

  async function stop() {
    live.current = false
    setRecording(false)
    onChange(await picker.stopRecording())
  }

  async function tryThese() {
    setTrying(true)
    try {
      setOutcomes(await picker.applySteps(steps))
    } finally {
      setTrying(false)
    }
  }

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
            disabled={disabled}
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

      {steps.length > 0 && (
        <ol className="flex flex-col gap-0.5">
          {steps.map((step, i) => (
            <li key={i} className="flex items-center gap-1.5 text-[11px]">
              <span className="w-4 shrink-0 text-right text-muted-foreground">{i + 1}</span>
              <Badge variant="outline" className="shrink-0 px-1 py-0 font-mono text-[10px]">
                {step.op}
              </Badge>
              <span className="min-w-0 flex-1 truncate text-muted-foreground">
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
                  onClick={() => onChange(steps.filter((_, j) => j !== i))}
                >
                  <Trash2 className="size-3" />
                </Button>
              )}
            </li>
          ))}
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
