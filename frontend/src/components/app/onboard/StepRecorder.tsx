import { useCallback, useEffect, useRef, useState } from 'react'
import {
  ChevronDown,
  ChevronUp,
  Circle,
  MousePointerClick,
  Pause,
  PlayCircle,
  Square,
  Trash2,
  TriangleAlert,
} from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { useToast } from '@/components/ui/toast'
import type { PreviewStep, StepOutcome } from '@/lib/picker/preview'
import type { StepIntent } from '@/lib/picker/protocol'
import { READ_ATTRIBUTES } from '@/lib/recipe/attributes'
import type { usePagePicker } from '@/hooks/usePagePicker'

/**
 * Record the route to a field the model could not find on its own.
 *
 * Pointing at an element answers *where is this?*. Some fields need the other
 * question — *how do I get to it?* — and no selector describes three clicks, a
 * scroll and a dismissal. This records that, and the recipe replays it.
 *
 * **A route is ordered and typed, not a list of clicks.** That is what the
 * panel is for. The extension had half of it already — `ElementDefinition`
 * tags each entry of one ordered list as something to click or something to
 * read — and a recording adds the ordering plus the distinction that only
 * replay cares about: a click that opens the section the field lives in is
 * load-bearing, and a click that closes a cookie banner is not. They replay
 * differently (see `StepIntent`), so the person has to be able to say which is
 * which, and the recorder has to guess well enough that they rarely need to.
 *
 * Two things about the UI follow from what recording is:
 *
 * - **The page must respond normally.** Unlike picking, nothing is swallowed
 *   and no overlay is drawn, so the person just works the page in the live view
 *   as they would anywhere. The panel polls for what has arrived so they can
 *   see it accumulating rather than trusting that it is. The one exception is
 *   *Pick element*, which pauses the recording precisely because the picker
 *   does swallow clicks — see `pickInRecording`.
 * - **"Try these" exists because a recording is a claim.** It replays the steps
 *   in the page, which is a rehearsal rather than replay (`preview.ts::runSteps`
 *   dispatches from page script, not the trusted CDP click) — so a step that
 *   fails here is a genuine problem worth seeing before it is submitted, even
 *   though one that passes is not yet proof.
 */

/** Slow enough not to flood the session, fast enough to feel live. */
const POLL_MS = 700

/** How each intent reads in the row, and what it means for replay. */
const INTENTS: { value: StepIntent; label: string; hint: string }[] = [
  { value: 'reveal', label: 'reveal', hint: 'The field is not there without it. A miss is reported.' },
  { value: 'dismiss', label: 'dismiss', hint: 'A banner or a modal ×. May be absent on a run; skipped quietly.' },
  { value: 'settle', label: 'wait', hint: 'Wait for what the click above revealed, before reading.' },
  { value: 'select', label: 'select', hint: 'The element the value is read from.' },
  { value: 'incidental', label: 'incidental', hint: 'Scrolling and strays. Kept for readability; safe to drop.' },
]

const INTENT_TONE: Record<StepIntent, string> = {
  reveal: 'border-accent/50 text-accent',
  dismiss: 'border-warning/50 text-warning',
  settle: 'border-muted-foreground/40 text-muted-foreground',
  select: 'border-success/50 text-success',
  incidental: 'border-muted-foreground/30 text-muted-foreground',
}

function intentOf(step: PreviewStep): StepIntent {
  // Absent means `reveal`, which is what every step recorded before intents
  // existed was in practice. Mirrors `_intent_of` in `assist.py`.
  return step.intent ?? 'reveal'
}

/**
 * What the picker's classifier decided this select reads, before any override.
 *
 * Mirrors `EXTRACTION_TYPE` in `fromPick.ts`, which is what actually applies
 * it — this only has to show the same default in the chooser, so that landing
 * on an `<a>` reads "Link" rather than "Text" with no indication that the
 * binding disagrees.
 */
function pickedAttribute(step: PreviewStep): string {
  const kind = (step.pick as { extractionType?: string } | undefined)?.extractionType
  if (kind === 'link' || kind === 'link_array') return 'href'
  if (kind === 'image' || kind === 'image_array') return 'src'
  return 'text'
}

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
  const [outcomes, setOutcomes] = useState<Record<number, StepOutcome> | null>(null)
  const [trying, setTrying] = useState(false)
  const [paused, setPaused] = useState(false)
  const [navigated, setNavigated] = useState(false)
  const [full, setFull] = useState(false)
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
        .pollRecording()
        .then((state) => {
          if (!live.current) return
          setPaused(state.paused)
          setNavigated(state.navigated)
          setFull(state.full)
          if (state.steps.length) onChangeRef.current(field, state.steps)
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
    setNavigated(false)
    setFull(false)
    setPaused(false)
    try {
      await picker.startRecording()
    } catch (err) {
      // Reported BEFORE the old route is discarded. Clearing first meant an
      // expired session -- which is exactly when `startRecording` rejects --
      // destroyed a recording the person could no longer make again, and
      // handed them a toast about it.
      toast({
        title: 'Could not start recording',
        description: err instanceof Error ? err.message : String(err),
        variant: 'destructive',
      })
      return
    }
    onChange(field, [])
    onRecordingChange(field)
  }, [picker, field, onChange, onRecordingChange, toast])

  const stop = useCallback(async () => {
    live.current = false
    onRecordingChange(null)
    setPaused(false)
    try {
      const state = await picker.stopRecording()
      setNavigated(state.navigated)
      setFull(state.full)
      onChange(field, state.steps)
    } catch (err) {
      toast({
        title: 'Could not read the recording back',
        description: err instanceof Error ? err.message : String(err),
        variant: 'destructive',
      })
    }
  }, [picker, field, onChange, onRecordingChange, toast])

  async function pickInto(action: 'extract' | 'click') {
    try {
      await picker.pickInRecording(action)
      setPaused(true)
    } catch (err) {
      toast({
        title: 'Could not start picking',
        description: err instanceof Error ? err.message : String(err),
        variant: 'destructive',
      })
    }
  }

  async function tryThese() {
    setTrying(true)
    try {
      // Only what the server will actually keep -- see `keepable` -- but the
      // outcomes have to come back addressed by ORIGINAL index. Indexing the
      // returned array by row number silently shifted every badge after the
      // first dropped step onto the wrong row, which is worse than no badge:
      // it reports a failure against a step that did not fail.
      const kept: PreviewStep[] = []
      const origin: number[] = []
      steps.forEach((step, i) => {
        if (!keepable(step)) return
        kept.push(step)
        origin.push(i)
      })
      const got = await picker.applySteps(kept)
      const byRow: Record<number, StepOutcome> = {}
      got.forEach((outcome, i) => {
        if (origin[i] !== undefined) byRow[origin[i]] = outcome
      })
      setOutcomes(byRow)
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

  function edit(index: number, patch: Partial<PreviewStep>) {
    onChange(
      field,
      steps.map((step, i) => (i === index ? { ...step, ...patch } : step)),
    )
  }

  function move(index: number, by: -1 | 1) {
    const to = index + by
    if (to < 0 || to >= steps.length) return
    const next = [...steps]
    ;[next[index], next[to]] = [next[to], next[index]]
    onChange(field, next)
    // The badges were addressed by the old positions and would now be wrong.
    setOutcomes(null)
  }

  const dropped = steps.filter((s) => !savable(s)).length
  const busyElsewhere = recordingField !== null && !recording

  return (
    <div className="flex flex-col gap-1.5 rounded-md border border-dashed border-border p-2">
      <div className="flex items-center gap-1.5">
        <Circle
          className={`size-3.5 shrink-0 ${
            recording && !paused
              ? 'animate-pulse fill-destructive text-destructive'
              : recording
                ? 'fill-warning text-warning'
                : 'text-muted-foreground'
          }`}
        />
        <span className="text-[11px] font-medium">…or show it how to get there</span>
      </div>
      <p className="text-[11px] text-muted-foreground">
        {paused
          ? 'Paused — click the element you want in the view on the left. Recording picks back up straight after.'
          : recording
            ? 'Recording — work the page in the view on the left. Use Pick element when you reach the value itself.'
            : 'Record the clicks that reveal the field, and pick the value where it appears. The recipe replays the whole route on every run.'}
      </p>

      <div className="flex flex-wrap items-center gap-1">
        {recording ? (
          <>
            <Button size="sm" variant="destructive" className="h-6 px-2 text-[11px]" onClick={() => void stop()}>
              <Square className="size-3" />
              Stop
            </Button>
            <Button
              size="sm"
              variant="outline"
              className="h-6 px-2 text-[11px]"
              disabled={paused}
              title="Pause and point at the value this route leads to"
              onClick={() => void pickInto('extract')}
            >
              <MousePointerClick className="size-3" />
              Pick value
            </Button>
            <Button
              size="sm"
              variant="outline"
              className="h-6 px-2 text-[11px]"
              disabled={paused}
              title="Pause and point at a control to click — a close button, a tab, a Show more"
              onClick={() => void pickInto('click')}
            >
              <Pause className="size-3" />
              Pick a click
            </Button>
          </>
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

      {navigated && (
        // `Recorder` has tracked this since it was written and nothing could
        // ask: `didNavigate` was absent from `PickerApi`. Every step after a
        // navigation targets a different document, and because reveal steps
        // replay `on_error: continue` the route then fails in total silence.
        <Note tone="destructive">
          The page navigated while this was recording, so the steps after that point are against a
          different page. Reload and record it again from the top.
        </Note>
      )}

      {full && (
        <Note tone="warning">
          This recording hit its 60-step limit and stopped capturing. Drop what is not needed, or
          record a shorter route.
        </Note>
      )}

      {dropped > 0 && (
        // The rehearsal used to run the whole client-side list while the server
        // silently discarded some of it (`parse_recorded_steps` drops unknown
        // ops and undispatchable targets), so "Try these" could pass on a route
        // that would be stored with holes in it.
        <Note tone="warning">
          {dropped === 1 ? 'One action was' : `${dropped} actions were`} not recordable &mdash; no
          stable selector for what was clicked. They are shown struck through and will not be saved.
        </Note>
      )}

      {steps.length > 0 && (
        <ol className="flex flex-col gap-0.5">
          {steps.map((step, i) => {
            const kept = savable(step)
            const intent = intentOf(step)
            const outcome = outcomes?.[i]
            return (
              <li key={i} className="flex items-center gap-1.5 text-[11px]">
                <span className="w-4 shrink-0 text-right text-muted-foreground">{i + 1}</span>
                <select
                  className={`h-5 shrink-0 rounded border bg-transparent px-1 text-[10px] font-medium ${INTENT_TONE[intent]}`}
                  value={intent}
                  disabled={recording}
                  title={INTENTS.find((o) => o.value === intent)?.hint}
                  onChange={(e) => edit(i, { intent: e.target.value as StepIntent })}
                >
                  {INTENTS.map((o) => (
                    <option key={o.value} value={o.value}>
                      {o.label}
                    </option>
                  ))}
                </select>
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
                  title={step.selector}
                >
                  {step.label || step.text || step.selector || '—'}
                </span>
                {intent === 'select' && (
                  // The other half of the interaction, and the half the panel
                  // could not express before: which attribute the value is
                  // read off. The classifier's guess arrives on the payload;
                  // this is how it gets overruled.
                  <select
                    className="h-5 shrink-0 rounded border border-success/40 bg-transparent px-1 text-[10px] text-success"
                    value={step.attribute ?? pickedAttribute(step)}
                    disabled={recording}
                    title="What to read off this element"
                    onChange={(e) => edit(i, { attribute: e.target.value })}
                  >
                    {READ_ATTRIBUTES.map((a) => (
                      <option key={a.value} value={a.value} title={a.hint}>
                        {a.label}
                      </option>
                    ))}
                  </select>
                )}
                {outcome && (
                  <Badge
                    variant={
                      outcome.status === 'ok'
                        ? 'success'
                        : outcome.status === 'failed'
                          ? 'destructive'
                          : 'warning'
                    }
                    className="shrink-0 px-1 py-0 text-[10px]"
                    title={outcome.detail}
                  >
                    {outcome.status}
                  </Badge>
                )}
                {!recording && (
                  <>
                    <Button
                      size="sm"
                      variant="ghost"
                      className="size-5 shrink-0 p-0"
                      title="Move earlier"
                      disabled={i === 0}
                      onClick={() => move(i, -1)}
                    >
                      <ChevronUp className="size-3" />
                    </Button>
                    <Button
                      size="sm"
                      variant="ghost"
                      className="size-5 shrink-0 p-0"
                      title="Move later"
                      disabled={i === steps.length - 1}
                      onClick={() => move(i, 1)}
                    >
                      <ChevronDown className="size-3" />
                    </Button>
                    <Button
                      size="sm"
                      variant="ghost"
                      className="size-5 shrink-0 p-0"
                      title="Drop this step"
                      onClick={() => onChange(field, steps.filter((_, j) => j !== i))}
                    >
                      <Trash2 className="size-3" />
                    </Button>
                  </>
                )}
              </li>
            )
          })}
        </ol>
      )}

      {outcomes && Object.values(outcomes).some((o) => o.status === 'failed') && (
        <Note tone="warning">
          A step failed when replayed. It may depend on something your session already had open —
          reload the page and record it again from the top.
        </Note>
      )}
    </div>
  )
}

function Note({ tone, children }: { tone: 'warning' | 'destructive'; children: React.ReactNode }) {
  return (
    <p
      className={`flex items-start gap-1.5 rounded px-2 py-1 text-[11px] text-muted-foreground ${
        tone === 'destructive' ? 'bg-destructive/10' : 'bg-warning/10'
      }`}
    >
      <TriangleAlert
        className={`mt-0.5 size-3 shrink-0 ${
          tone === 'destructive' ? 'text-destructive' : 'text-warning'
        }`}
      />
      {children}
    </p>
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
 *
 * `select` is deliberately absent: it is the binding, not an action, and the
 * server consumes it rather than replaying it. It is kept out of "Try these"
 * for the same reason — there is nothing to rehearse.
 */
const RECORDABLE_OPS = new Set([
  'click',
  'fill',
  'select_option',
  'press',
  'scroll',
  'scroll_into_view',
  'hover',
  'wait',
  'wait_for_selector',
])

function keepable(step: PreviewStep): boolean {
  if (!RECORDABLE_OPS.has(step.op)) return false
  if (step.selector) return true
  return step.op === 'press' || step.op === 'scroll' || step.op === 'wait'
}

/**
 * Whether the server will keep this entry at all.
 *
 * Wider than `keepable`, and the two must not be conflated. A `select` is not
 * replayable — it is the binding the route exists to reach — so it is excluded
 * from the rehearsal, but showing it struck through as "not recordable" would
 * tell the person the most important row in their route was about to be thrown
 * away.
 */
function savable(step: PreviewStep): boolean {
  if (intentOf(step) === 'select') return Boolean(step.pick)
  return keepable(step)
}
