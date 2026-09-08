import { useMemo, useState } from 'react'
import { HelpCircle, SkipForward } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { LiveViewPanel } from '@/components/app/LiveViewPanel'
import { PickerControls } from '@/components/app/wizard/PickerControls'
import { usePagePicker } from '@/hooks/usePagePicker'
import { useSessionsList } from '@/hooks/useSessionsList'
import { useSubmitAssist } from '@/hooks/useRecipes'
import { useToast } from '@/components/ui/toast'
import { detailPickToDraft } from '@/lib/recipe/fromPick'
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
  const [answers, setAnswers] = useState<Record<string, RecipeResolution>>({})

  // The run's own session is the one showing the stuck page. It is named after
  // the run, which is how it is found among whatever else is open.
  const { data } = useSessionsList()
  const sessions = (data?.sessions ?? []).filter((s) => s.state === 'active')
  const runSession = sessions.find((s) => s.session_id === `recipe-run-${runId}`)
  const [sessionId, setSessionId] = useState<string | null>(null)
  const active = sessionId ?? runSession?.session_id ?? sessions[0]?.session_id ?? null

  const [selected, setSelected] = useState<string>(asks[0]?.field ?? '')
  const picker = usePagePicker(active)

  const answered = useMemo(() => Object.keys(answers).length, [answers])

  async function pickFor(fieldName: string) {
    const payload = await picker.pick('detail')
    if (!payload) return
    const draft = detailPickToDraft(payload)
    if (draft.candidates.length === 0) {
      toast({ title: 'No usable selector for that element' })
      return
    }
    setAnswers((prev) => ({
      ...prev,
      [fieldName]: {
        field: fieldName,
        action: 'pick',
        locators: draft.candidates.map((c) => c.locator as unknown as Record<string, unknown>),
      },
    }))
    toast({
      title: `${fieldName}: ${draft.candidates.length} candidates`,
      description: draft.preview ? `Reads "${draft.preview}"` : undefined,
    })
  }

  function send() {
    submit.mutate(Object.values(answers), {
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
              answer={answers[ask.field]}
              isSelected={selected === ask.field}
              canPick={active !== null}
              pickerStatus={picker.status}
              onSelect={() => setSelected(ask.field)}
              onPick={() => void pickFor(ask.field)}
              onCancelPick={picker.cancel}
              onRefinePick={picker.refine}
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
              onClear={() =>
                setAnswers((prev) => {
                  const next = { ...prev }
                  delete next[ask.field]
                  return next
                })
              }
            />
          ))}
        </div>
      </div>
    </div>
  )
}

function AskRow({
  ask,
  answer,
  isSelected,
  canPick,
  pickerStatus,
  onSelect,
  onPick,
  onCancelPick,
  onRefinePick,
  onDescribe,
  onSkip,
  onClear,
}: {
  ask: PendingAsk
  answer: RecipeResolution | undefined
  isSelected: boolean
  canPick: boolean
  pickerStatus: ReturnType<typeof usePagePicker>['status']
  onSelect: () => void
  onPick: () => void
  onCancelPick: () => void
  onRefinePick: (key: 'ArrowUp' | 'ArrowDown' | 'Enter') => void
  onDescribe: (hint: string) => void
  onSkip: () => void
  onClear: () => void
}) {
  const [hint, setHint] = useState('')

  // A step that matched nothing is otherwise silent -- every reveal step is
  // `optional`/`on_error: continue` by construction -- so a field can be empty
  // because its selector is wrong OR because the click that was meant to reveal
  // it never ran. Those need opposite fixes, and only the trace tells them
  // apart.
  const failedSteps = ask.step_trace.filter((s) => s.status === 'failed')

  return (
    <div
      className={`flex flex-col gap-2 rounded-md border p-3 ${
        isSelected ? 'border-accent' : 'border-border'
      }`}
      onClick={onSelect}
    >
      <div className="flex items-center gap-2">
        <span className="font-mono text-xs font-medium">{ask.field}</span>
        <Badge variant={ask.kind === 'rejected' ? 'warning' : 'outline'}>
          {ask.kind === 'rejected' ? 'looked wrong' : 'not found'}
        </Badge>
        {answer && (
          <Badge variant="success" className="ml-auto">
            {answer.action === 'pick' ? 'picked' : answer.action === 'skip' ? 'skipped' : 'described'}
          </Badge>
        )}
      </div>

      <p className="text-xs text-muted-foreground">{ask.reason}</p>

      {failedSteps.length > 0 && (
        <p className="rounded bg-warning/10 px-2 py-1 text-[11px] text-muted-foreground">
          {failedSteps.length === 1 ? 'A step' : `${failedSteps.length} steps`} before this field
          did not run &mdash; it may be hidden rather than missing.
        </p>
      )}

      {answer ? (
        <Button size="sm" variant="ghost" className="self-start" onClick={onClear}>
          Change
        </Button>
      ) : (
        <div className="flex flex-col gap-2">
          <PickerControls
            status={pickerStatus}
            label="Point at it"
            disabled={!canPick}
            onStart={onPick}
            onCancel={onCancelPick}
            onRefine={onRefinePick}
          />
          <div className="flex items-center gap-1.5">
            <HelpCircle className="size-3.5 shrink-0 text-muted-foreground" />
            <Input
              className="h-7 text-xs"
              placeholder="or say where it is — 'inside the Details accordion'"
              value={hint}
              onChange={(e) => setHint(e.target.value)}
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
