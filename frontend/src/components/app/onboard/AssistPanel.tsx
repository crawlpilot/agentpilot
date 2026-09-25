import { useCallback, useMemo, useState } from 'react'
import { Check, Frame, HelpCircle, SkipForward, TriangleAlert } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { LiveViewPanel } from '@/components/app/LiveViewPanel'
import { PickerControls } from '@/components/app/wizard/PickerControls'
import { usePagePicker } from '@/hooks/usePagePicker'
import { useSessionsList } from '@/hooks/useSessionsList'
import { useAssistHeartbeat, useSubmitAssist } from '@/hooks/useRecipes'
import { useToast } from '@/components/ui/toast'
import { StepRecorder } from './StepRecorder'
import {
  detailPickToDraft,
  readAttribute,
  scopeFromPick,
  setReadAttribute,
  toPreviewFields,
} from '@/lib/recipe/fromPick'
import { READ_ATTRIBUTES, attributeHint } from '@/lib/recipe/attributes'
import type { PreviewStep } from '@/lib/picker/preview'
import type { PickPayload } from '@/lib/picker/protocol'
import type { FieldDraft } from '@/lib/recipe/fromPick'
import type { Candidate, FieldSpec } from '@/lib/recipe/types'
import type { PendingAsk, RecipeResolution } from '@/lib/api/types'

/**
 * The build stopped and is asking.
 *
 * Its browser session is still open on the page it got stuck on, with whatever
 * the run had opened still open -- which is why the answer can be *point at
 * it* rather than *describe it*. The picker, the live view and the locator
 * derivation are the studio's, unchanged; only the question is new.
 *
 * The session is a wasting asset: it holds a worker slot, a warm identity and
 * a proxy pin, and the park expires. So the panel says so rather than letting
 * someone wander off mid-answer.
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
  // Answering an ask properly is a reload, a recording, a pick and a look at
  // what it read. The park has to be bounded -- it holds a worker slot, a warm
  // identity, a browser and a proxy pin -- so this says somebody is still here,
  // rather than the ceiling being the budget for doing the work.
  useAssistHeartbeat(recipeId, runId, asks.length > 0)
  const [answers, setAnswers] = useState<Record<string, RecipeResolution>>({})
  // A pick is kept as the DRAFT the picker derived, not as a finished
  // resolution, because the draft is what the attribute chooser rewrites and
  // what carries the type and cleanup the picker worked out. Flattening it to
  // locators at pick time is what silently dropped `url_resolve` from every
  // hand-corrected URL field.
  const [picks, setPicks] = useState<
    Record<string, { candidates: Candidate[]; spec: FieldSpec; preview: string }>
  >({})
  // What a scope pick actually selected, so the person can see they hit the
  // heading before they submit rather than after the run comes back with it.
  const [scoped, setScoped] = useState<
    Record<string, { selector: string; matched: number; preview: string }>
  >({})

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

  const [selected, setSelected] = useState<string>(asks[0]?.field ?? '')
  // The route recorded for each field, kept here rather than in the row so a
  // recording survives the row re-rendering under it.
  const [recorded, setRecorded] = useState<Record<string, PreviewStep[]>>({})
  // Which field is recording, if any. The in-page recorder is a SINGLE object
  // (`entry.ts` constructs one) and `start()` clears its buffer, so two rows
  // recording at once means the second wipes the first and the first's poller
  // then collects the second's steps.
  const [recordingField, setRecordingField] = useState<string | null>(null)
  const picker = usePagePicker(active)

  /**
   * Everything to send: what was picked/described/skipped, with each field's
   * recording folded into it.
   *
   * Assembled here rather than as the recording happens, so a recording in
   * progress does not count as a finished answer and collapse the row it is
   * being made in.
   *
   * **A recording and a pick are one answer, not two.** They used to compete --
   * an explicit pick discarded the route recorded beside it -- and that made
   * the case they are both for unanswerable. A specifications accordion needs
   * "open this, then read that": the pick alone binds against a section replay
   * loads shut, and the route alone throws away the region the person went to
   * the trouble of pointing at. `apply_resolutions` runs the steps first and
   * then binds, so both halves travel together.
   *
   * `describe` and `skip` take no route: one is handed to the model as words,
   * the other removes the field.
   */
  const resolutions = useMemo(() => {
    const out = { ...answers }
    for (const [field, draft] of Object.entries(picks)) {
      out[field] = {
        field,
        action: 'pick',
        locators: draft.candidates.map((c) => c.locator as unknown as Record<string, unknown>),
        // The type and cleanup the picker derived. `_bind` never wrote these,
        // so a manually picked `<a href>` kept relative URLs for ever.
        spec: draft.spec as unknown as Record<string, unknown>,
      }
    }
    for (const [field, steps] of Object.entries(recorded)) {
      if (!steps.length) continue
      const recording = steps.map(forWire) as unknown as Array<Record<string, unknown>>
      const answer = out[field]
      // A route that contains a `select` already names the element, so it is a
      // `pick` whose steps happen to describe how to reach it -- not a bare
      // `steps` answer the model has to search all over again. The step stays
      // in the array as the marker the server splits the route on, so it knows
      // which of these run before the binding and which run after it.
      const chosen = !answer ? selectInRoute(steps) : null
      if (chosen) {
        out[field] = {
          field,
          action: 'pick',
          locators: chosen.candidates.map((c) => c.locator as unknown as Record<string, unknown>),
          spec: chosen.spec as unknown as Record<string, unknown>,
          steps: recording,
        }
      } else if (!answer) {
        out[field] = { field, action: 'steps', steps: recording }
      } else if (answer.action === 'pick' || answer.action === 'scope') {
        out[field] = { ...answer, steps: recording }
      }
    }
    return out
  }, [answers, picks, recorded])

  const answered = useMemo(() => Object.keys(resolutions).length, [resolutions])

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

  async function scopeFor(fieldName: string, shape: 'one' | 'values' | 'map' | 'rows') {
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
      setAnswers((prev) => ({
        ...prev,
        [fieldName]: {
          field: fieldName,
          action: 'scope',
          locators: scope.locators as unknown as Array<Record<string, unknown>>,
          shape,
          html,
        },
      }))
      setScoped((prev) => ({ ...prev, [fieldName]: { ...scope, selector } }))
    } catch (err) {
      // `usePagePicker.pick` rethrows when the injection fails, and the call
      // sites discard the rejection (`void scopeFor(...)`). Unhandled, the
      // button simply looked dead -- which is exactly how an expired session
      // presented itself.
      pickFailed(err)
    }
  }

  async function pickFor(fieldName: string) {
    try {
      const payload = await picker.pick('detail')
      if (!payload) return
      const draft = detailPickToDraft(payload)
      if (draft.candidates.length === 0) {
        toast({ title: 'No usable selector for that element' })
        return
      }
      const preview = (await readBack(draft.candidates)) || draft.preview || ''
      setPicks((prev) => ({
        ...prev,
        [fieldName]: { candidates: draft.candidates, spec: draft.spec, preview },
      }))
    } catch (err) {
      pickFailed(err)
    }
  }

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

  /** Re-read the field through a different attribute. */
  async function changeAttribute(fieldName: string, attribute: string) {
    const draft = picks[fieldName]
    if (!draft) return
    const candidates = setReadAttribute(draft.candidates, attribute)
    const preview = await readBack(candidates)
    setPicks((prev) => ({ ...prev, [fieldName]: { ...draft, candidates, preview } }))
  }

  const setSteps = useCallback((fieldName: string, steps: PreviewStep[]) => {
    // Stable identity: `StepRecorder`'s polling effect depends on this, and an
    // inline arrow here rebuilt its interval on every parent render -- which is
    // every sessions poll and every heartbeat.
    setRecorded((prev) => ({ ...prev, [fieldName]: steps }))
  }, [])

  function clear(fieldName: string) {
    setAnswers((prev) => {
      const next = { ...prev }
      delete next[fieldName]
      return next
    })
    setPicks((prev) => {
      const next = { ...prev }
      delete next[fieldName]
      return next
    })
    setScoped((prev) => {
      const next = { ...prev }
      delete next[fieldName]
      return next
    })
  }

  function send() {
    submit.mutate(Object.values(resolutions), {
      onSuccess: (resp) =>
        toast({
          title: 'Sent',
          description: `Carrying on with ${resp.accepted.length} answered.`,
        }),
      onError: (err) =>
        toast({ title: 'Could not send', description: err.message, variant: 'destructive' }),
    })
  }

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
          The page below is where it stopped &mdash; pick on it directly.
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
        // Set by the hook and, until now, rendered nowhere -- so an injection
        // that failed left the panel looking idle.
        <p className="flex items-center gap-1.5 rounded bg-destructive/10 px-2 py-1 text-[11px] text-destructive">
          <TriangleAlert className="size-3.5 shrink-0" />
          {picker.error}
        </p>
      )}

      <div className="grid min-h-0 flex-1 grid-cols-[minmax(0,1fr)_22rem] gap-3">
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

        <div className="flex min-h-0 flex-col gap-2 overflow-y-auto">
          {asks.map((ask) => (
            <AskRow
              key={ask.field}
              ask={ask}
              answer={resolutions[ask.field]}
              isSelected={selected === ask.field}
              canPick={active !== null}
              pickerStatus={picker.status}
              onSelect={() => setSelected(ask.field)}
              onPick={() => void pickFor(ask.field)}
              onScope={(shape) => void scopeFor(ask.field, shape)}
              scoped={scoped[ask.field]}
              picked={picks[ask.field]}
              onAttribute={(value) => void changeAttribute(ask.field, value)}
              onCancelPick={picker.cancel}
              onRefinePick={picker.refine}
              picker={picker}
              steps={recorded[ask.field] ?? []}
              onSteps={setSteps}
              recordingField={recordingField}
              onRecordingChange={setRecordingField}
              onAccept={() =>
                setAnswers((prev) => ({
                  ...prev,
                  [ask.field]: { field: ask.field, action: 'accept' },
                }))
              }
              onDescribe={(hint) =>
                setAnswers((prev) => ({
                  ...prev,
                  [ask.field]: { field: ask.field, action: 'describe', hint },
                }))
              }
              onSkip={() =>
                setAnswers((prev) => ({
                  ...prev,
                  [ask.field]: { field: ask.field, action: 'skip' },
                }))
              }
              onClear={() => clear(ask.field)}
            />
          ))}
        </div>
      </div>
    </div>
  )
}

const SHAPE_LABEL = {
  one: 'one value',
  values: 'a list',
  map: 'key → value',
  rows: 'rows',
} as const

const SHAPE_HELP = {
  one: 'A single value somewhere in this region.',
  values: 'Several values of the same kind — image URLs, bullet points.',
  map: 'Labelled pairs whose keys come from the page — a specifications block.',
  rows: 'Repeating rows with the same columns.',
} as const

/**
 * One route step, minus what only the browser needed.
 *
 * `pick` is the entire enriched `PickPayload` — candidate chains, extracted
 * rows, inferred columns — and for a list pick that runs to tens of kilobytes.
 * It has already done its job by the time a route is submitted: `selectInRoute`
 * turned it into the locators and spec travelling alongside. The server wants
 * the `select` marker, so it knows where to cut the route, and nothing else
 * from it. Sending the payload would put it in the request body sixty times
 * over for no reader.
 */
function forWire(step: PreviewStep): PreviewStep {
  if (!step.pick) return step
  const { pick: _drop, ...rest } = step
  return rest
}

/**
 * The binding a route carries, if the person picked one while recording.
 *
 * The conversion is exactly the one the standalone pick button does --
 * `detailPickToDraft` -- so a value chosen mid-route gets the same derived type
 * and cleanup as one chosen on its own, rather than a second, poorer path to
 * the same thing. The attribute override the row offers is applied here,
 * because it is the last point at which the candidate chain still exists.
 *
 * The LAST select wins. A person who picks, looks at the preview and picks
 * again has corrected themselves, and the earlier row is visibly still in the
 * list for them to delete if they meant something else by it.
 */
function selectInRoute(steps: PreviewStep[]): FieldDraft | null {
  for (let i = steps.length - 1; i >= 0; i--) {
    const step = steps[i]
    if (step.intent !== 'select' || !step.pick) continue
    const draft = detailPickToDraft(step.pick as PickPayload)
    if (draft.candidates.length === 0) return null
    return step.attribute
      ? { ...draft, candidates: setReadAttribute(draft.candidates, step.attribute) }
      : draft
  }
  return null
}

/** Every action, so a `scope` or a `steps` answer is not labelled "described". */
const ACTION_LABEL: Record<RecipeResolution['action'], string> = {
  pick: 'picked',
  scope: 'region picked',
  steps: 'route recorded',
  describe: 'described',
  accept: 'kept',
  skip: 'skipped',
}

function truncate(text: string, at = 60): string {
  return text.length > at ? `${text.slice(0, at)}…` : text
}

function AskRow({
  ask,
  answer,
  isSelected,
  canPick,
  pickerStatus,
  onSelect,
  onPick,
  onScope,
  scoped,
  picked,
  onAttribute,
  onCancelPick,
  onRefinePick,
  onAccept,
  onDescribe,
  onSkip,
  onClear,
  picker,
  steps,
  onSteps,
  recordingField,
  onRecordingChange,
}: {
  ask: PendingAsk
  answer: RecipeResolution | undefined
  isSelected: boolean
  canPick: boolean
  pickerStatus: ReturnType<typeof usePagePicker>['status']
  onSelect: () => void
  onPick: () => void
  onScope: (shape: 'one' | 'values' | 'map' | 'rows') => void
  scoped: { selector: string; matched: number; preview: string } | undefined
  picked: { candidates: Candidate[]; spec: FieldSpec; preview: string } | undefined
  onAttribute: (value: string) => void
  onCancelPick: () => void
  onRefinePick: (key: 'ArrowUp' | 'ArrowDown' | 'Enter' | 'Unpin') => void
  onAccept: () => void
  onDescribe: (hint: string) => void
  onSkip: () => void
  onClear: () => void
  picker: ReturnType<typeof usePagePicker>
  steps: PreviewStep[]
  onSteps: (field: string, steps: PreviewStep[]) => void
  recordingField: string | null
  onRecordingChange: (field: string | null) => void
}) {
  const [hint, setHint] = useState('')

  // A step that matched nothing is otherwise silent -- every reveal step is
  // `optional`/`on_error: continue` by construction -- so a field can be empty
  // because its selector is wrong OR because the click that was meant to reveal
  // it never ran. Those need opposite fixes, and only the trace tells them
  // apart.
  const failedSteps = ask.step_trace.filter((s) => s.status === 'failed')
  const attribute = picked ? (readAttribute(picked.candidates) ?? 'text') : 'text'

  return (
    <div
      className={`flex flex-col gap-2 rounded-md border p-3 ${
        isSelected ? 'border-accent' : 'border-border'
      }`}
      onClick={onSelect}
    >
      <div className="flex items-center gap-2">
        <span className="font-mono text-xs font-medium">{ask.field}</span>
        <Badge variant={ask.kind === 'unresolved' ? 'outline' : 'warning'}>
          {ask.kind === 'rejected'
            ? 'looked wrong'
            : ask.kind === 'absent'
              ? 'not on this page'
              : 'not found'}
        </Badge>
        {answer && (
          <Badge variant="success" className="ml-auto">
            {ACTION_LABEL[answer.action]}
          </Badge>
        )}
      </div>

      <p className="text-xs text-muted-foreground">{ask.reason}</p>

      {ask.kind === 'rejected' && ask.value && (
        // The value the judgement is about. Without it this row asks somebody
        // to overrule a verdict it never showed them -- and the judge is a
        // model, so a description carrying "Imported from China" arrives here
        // rejected and correct.
        <div className="flex flex-col gap-1 rounded bg-muted/50 px-2 py-1">
          <span className="text-[11px] text-muted-foreground">It read:</span>
          <span className="text-[11px]">“{truncate(ask.value, 300)}”</span>
        </div>
      )}

      {ask.kind === 'absent' && (
        // A different question from the other two. It stopped looking on
        // purpose: searching harder for something that is not there is what
        // produced a wrong element every round until the budget ran out.
        <p className="rounded bg-muted/50 px-2 py-1 text-[11px] text-muted-foreground">
          It stopped looking rather than keep returning a different wrong element each
          time. If this really is on the page, point at the section it&rsquo;s in &mdash;
          otherwise drop it.
        </p>
      )}

      {failedSteps.length > 0 && (
        <p className="rounded bg-warning/10 px-2 py-1 text-[11px] text-muted-foreground">
          {failedSteps.length === 1 ? 'A step' : `${failedSteps.length} steps`} before this field
          did not run &mdash; it may be hidden rather than missing.
        </p>
      )}

      {ask.tried && (
        // What has already been ruled out, straight from the verifier. `reason`
        // says what is wrong now; this says what was tried, which is the
        // difference between being asked "find this" and being shown that the
        // last attempt read the section heading.
        <details className="rounded bg-muted/50 px-2 py-1 text-[11px] text-muted-foreground">
          <summary className="cursor-pointer select-none">What was already tried</summary>
          <pre className="mt-1 overflow-x-auto whitespace-pre-wrap font-mono text-[10px]">
            {ask.tried}
          </pre>
        </details>
      )}

      {answer ? (
        <div className="flex flex-col gap-2">
          {picked && (
            // What the pick reads, and through which attribute. The classifier
            // guesses the attribute from the element's kind, and its guess used
            // to be final: there was nowhere in this flow to say that an `<a>`
            // should be read as `href`.
            <div className="flex flex-col gap-1 rounded bg-muted/50 px-2 py-1">
              <div className="flex items-center gap-1.5">
                <span className="text-[11px] text-muted-foreground">Reads</span>
                <Select value={attribute} onValueChange={onAttribute}>
                  <SelectTrigger className="h-6 w-36 text-[11px]">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {READ_ATTRIBUTES.map((a) => (
                      <SelectItem key={a.value} value={a.value}>
                        {a.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <span className="text-[11px] text-muted-foreground">
                {attributeHint(attribute)}
              </span>
              <span className="text-[11px]">
                {picked.preview ? `→ “${truncate(picked.preview, 80)}”` : '→ reads nothing'}
              </span>
            </div>
          )}
          <Button size="sm" variant="ghost" className="self-start" onClick={onClear}>
            Change
          </Button>
        </div>
      ) : (
        <div className="flex flex-col gap-2">
          {ask.kind === 'rejected' && ask.value && (
            <Button size="sm" variant="outline" className="self-start" onClick={onAccept}>
              <Check className="size-3.5" />
              Keep this value
            </Button>
          )}

          <PickerControls
            status={pickerStatus}
            label="Point at the value"
            disabled={!canPick}
            onStart={onPick}
            onCancel={onCancelPick}
            onRefine={onRefinePick}
          />

          <div className="flex flex-col gap-1 rounded-md border border-dashed border-border p-2">
            <div className="flex items-center gap-1.5">
              <Frame className="size-3.5 shrink-0 text-muted-foreground" />
              <span className="text-[11px] font-medium">…or point at the section it&rsquo;s in</span>
            </div>
            <p className="text-[11px] text-muted-foreground">
              Use <b>↑</b> to grow the selection past the heading until it covers the whole
              block. A heading is not the value &mdash; select the region and the model finds
              the value inside it.
            </p>
            <div className="flex flex-wrap gap-1">
              {(['one', 'values', 'map', 'rows'] as const).map((shape) => (
                <Button
                  key={shape}
                  size="sm"
                  variant="outline"
                  className="h-6 px-2 text-[11px]"
                  disabled={!canPick}
                  onClick={() => onScope(shape)}
                  title={SHAPE_HELP[shape]}
                >
                  {SHAPE_LABEL[shape]}
                </Button>
              ))}
            </div>
            {scoped && (
              <div className="flex flex-col gap-0.5 rounded bg-muted/50 px-2 py-1">
                <code className="truncate text-[11px]" title={scoped.selector}>
                  {scoped.selector}
                </code>
                <span className="text-[11px] text-muted-foreground">
                  matches {scoped.matched || 1} element{scoped.matched === 1 ? '' : 's'}
                  {scoped.preview ? ` · reads “${truncate(scoped.preview)}”` : ''}
                </span>
                {scoped.preview && scoped.preview.length < 40 && (
                  // The exact Walmart mistake: a short reading means the
                  // selection is probably still on the label, not the block.
                  <span className="text-[11px] text-warning">
                    That looks like a heading. Press ↑ to include the content under it.
                  </span>
                )}
              </div>
            )}
          </div>
          <StepRecorder
            picker={picker}
            field={ask.field}
            disabled={!canPick}
            steps={steps}
            onChange={onSteps}
            recordingField={recordingField}
            onRecordingChange={onRecordingChange}
          />

          <div className="flex items-center gap-1.5">
            <HelpCircle className="size-3.5 shrink-0 text-muted-foreground" />
            <Input
              className="h-7 text-xs"
              placeholder="or say where it is — 'inside the Details accordion'"
              value={hint}
              onChange={(e) => setHint(e.target.value)}
              // Blur commits too: typing a hint and clicking Continue used to
              // lose it, because only Enter recorded the answer.
              onBlur={() => hint.trim() && onDescribe(hint.trim())}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && hint.trim()) onDescribe(hint.trim())
              }}
            />
          </div>
          <Button size="sm" variant="ghost" className="self-start" onClick={onSkip}>
            <SkipForward className="size-3.5" />
            Don&rsquo;t collect this
          </Button>
        </div>
      )}
    </div>
  )
}
