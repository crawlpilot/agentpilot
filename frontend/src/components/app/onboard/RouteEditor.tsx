import { useState } from 'react'
import {
  ChevronDown,
  ChevronUp,
  Eye,
  MousePointerClick,
  PlayCircle,
  Trash2,
  TriangleAlert,
} from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { useToast } from '@/components/ui/toast'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { isRead, splitAtRead, stepForPick } from '@/lib/recipe/assistRoute'
import { READ_ATTRIBUTES } from '@/lib/recipe/attributes'
import type { PreviewStep, StepOutcome } from '@/lib/picker/preview'
import type { usePagePicker } from '@/hooks/usePagePicker'

/**
 * Build the way to a field by pointing at things, in order.
 *
 * *Click this, then click that, then read this.* The extension's model —
 * `ElementDefinition { selectors, action: 'extract' | 'click' }`, one ordered
 * list — and it replaced a recorder that sat here. Recording captured what the
 * DOM emitted rather than what the person meant: stray clicks, scrolls, entries
 * with no stable selector, a sixty-step cap that truncated in silence. You
 * could not tell what it had caught, so you could not trust the route. Every
 * row here is an element somebody deliberately pointed at.
 *
 * Removing it took a lot with it, and that is the point: there is no in-page
 * recorder to own, so no panel-wide ownership state, no poll loop, no pause,
 * and no way for one half-finished answer to lock every other ask on the
 * screen. A pick is a single request/response `pick()` call.
 *
 * **Position is the contract.** Clicks before the read are how you reach the
 * value; clicks after it are how you tidy up. `splitAtRead` cuts there and
 * `assist.py::split_route` cuts the same array server-side, which is what lets
 * a trailing "close the modal" become the group's teardown rather than more
 * setup that would run before the binding and shut the value away.
 */
export function RouteEditor({
  picker,
  route,
  disabled,
  onChange,
}: {
  picker: ReturnType<typeof usePagePicker>
  route: PreviewStep[]
  disabled: boolean
  onChange: (route: PreviewStep[]) => void
}) {
  const { toast } = useToast()
  const [outcomes, setOutcomes] = useState<Record<number, StepOutcome> | null>(null)
  const [trying, setTrying] = useState(false)
  const picking = picker.status !== 'idle'

  async function add(action: 'extract' | 'click') {
    try {
      const payload = await picker.pick('detail', action)
      // Cancelled. Backing out of choosing an element is not an edit to the
      // rows around it.
      if (!payload) return
      onChange([...route, stepForPick(payload)])
      setOutcomes(null)
    } catch (err) {
      toast({
        title: 'Could not pick on the page',
        description: err instanceof Error ? err.message : String(err),
        variant: 'destructive',
      })
    }
  }

  async function tryThese() {
    setTrying(true)
    try {
      // The read is not replayable -- it is the binding this route exists to
      // reach -- so it is excluded, and the outcomes come back addressed by
      // ORIGINAL index. Indexing the returned array by row number would shift
      // every badge after the read onto the wrong row, which is worse than no
      // badge: it reports a failure against a step that did not fail.
      const kept: PreviewStep[] = []
      const origin: number[] = []
      route.forEach((step, i) => {
        if (isRead(step)) return
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
    onChange(route.map((step, i) => (i === index ? { ...step, ...patch } : step)))
  }

  function move(index: number, by: -1 | 1) {
    const to = index + by
    if (to < 0 || to >= route.length) return
    const next = [...route]
    ;[next[index], next[to]] = [next[to], next[index]]
    onChange(next)
    // The badges were addressed by the old positions.
    setOutcomes(null)
  }

  const { read, after } = splitAtRead(route)
  const unusable = route.filter((s) => !s.selector).length

  return (
    <div className="flex flex-col gap-2">
      <p className="text-[11px] text-muted-foreground">
        Point at what it takes to reach the value, in order &mdash; then at the value itself.
        Anything you point at after it is treated as tidying up.
      </p>

      {route.length > 0 && (
        <ol className="flex flex-col gap-1">
          {route.map((step, i) => {
            const reading = isRead(step)
            const outcome = outcomes?.[i]
            const teardown = after.includes(step)
            return (
              <li key={i} className="flex flex-col gap-0.5">
                <div className="flex items-center gap-1.5 text-[11px]">
                  <span className="w-4 shrink-0 text-right text-muted-foreground">{i + 1}</span>
                  <Badge
                    variant="outline"
                    className={`shrink-0 px-1 py-0 text-[10px] ${
                      reading
                        ? 'border-success/50 text-success'
                        : teardown
                          ? 'border-muted-foreground/40 text-muted-foreground'
                          : 'border-accent/50 text-accent'
                    }`}
                  >
                    {reading ? 'read' : teardown ? 'then close' : 'click'}
                  </Badge>
                  <span
                    className={`min-w-0 flex-1 truncate ${
                      step.selector ? 'text-muted-foreground' : 'text-destructive line-through'
                    }`}
                    title={step.selector ?? 'no stable selector'}
                  >
                    {step.text || step.selector || '—'}
                  </span>

                  {reading ? (
                    // What to read off the element. The classifier guesses from
                    // the element's kind, and its guess used to be final --
                    // there was nowhere in this flow to say an `<a>` should be
                    // read as its href.
                    <Select
                      value={step.attribute ?? pickedAttribute(step)}
                      onValueChange={(v) => edit(i, { attribute: v })}
                    >
                      <SelectTrigger className="h-5 w-24 shrink-0 text-[10px]">
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
                  ) : (
                    // The cookie-banner case. A dismissal that did not appear
                    // this time is not a failed run; the click that opens the
                    // section the field lives in is, and replaying both as
                    // optional is what made a broken reveal silent.
                    <label
                      className="flex shrink-0 cursor-pointer items-center gap-1 text-[10px] text-muted-foreground"
                      title="Replay will carry on if this element is not there"
                    >
                      <input
                        type="checkbox"
                        className="size-3"
                        checked={step.intent === 'dismiss'}
                        onChange={(e) =>
                          edit(i, { intent: e.target.checked ? 'dismiss' : 'reveal' })
                        }
                      />
                      may not appear
                    </label>
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

                  <Button
                    size="sm" variant="ghost" className="size-5 shrink-0 p-0"
                    title="Move earlier" disabled={i === 0} onClick={() => move(i, -1)}
                  >
                    <ChevronUp className="size-3" />
                  </Button>
                  <Button
                    size="sm" variant="ghost" className="size-5 shrink-0 p-0"
                    title="Move later" disabled={i === route.length - 1}
                    onClick={() => move(i, 1)}
                  >
                    <ChevronDown className="size-3" />
                  </Button>
                  <Button
                    size="sm" variant="ghost" className="size-5 shrink-0 p-0"
                    title="Drop this" onClick={() => onChange(route.filter((_, j) => j !== i))}
                  >
                    <Trash2 className="size-3" />
                  </Button>
                </div>
                {reading && step.text && (
                  <span className="pl-7 text-[11px]">&rarr; &ldquo;{step.text}&rdquo;</span>
                )}
              </li>
            )
          })}
        </ol>
      )}

      {unusable > 0 && (
        <Note>
          {unusable === 1 ? 'One row has' : `${unusable} rows have`} no stable selector for what
          was pointed at. They are struck through and will not be saved.
        </Note>
      )}

      <div className="flex flex-wrap items-center gap-1">
        <Button
          size="sm" variant="outline" className="h-6 px-2 text-[11px]"
          disabled={disabled || picking}
          title="Point at something that has to be clicked to get to the value"
          onClick={() => void add('click')}
        >
          <MousePointerClick className="size-3" />
          Click this
        </Button>
        <Button
          size="sm" variant={read ? 'outline' : 'default'} className="h-6 px-2 text-[11px]"
          disabled={disabled || picking}
          title={read ? 'Point at a different value — the last one wins' : 'Point at the value itself'}
          onClick={() => void add('extract')}
        >
          <Eye className="size-3" />
          {read ? 'Read something else' : 'Read this'}
        </Button>
        {route.length > 0 && (
          <Button
            size="sm" variant="outline" className="h-6 px-2 text-[11px]"
            disabled={disabled || trying || picking}
            title="Replay the clicks in the page now, so one that does not work is visible before you send it"
            onClick={() => void tryThese()}
          >
            <PlayCircle className="size-3" />
            {trying ? 'Trying…' : 'Try these'}
          </Button>
        )}
      </div>

      {picking && (
        <p className="text-[11px] text-accent">Click an element in the page on the left.</p>
      )}

      {outcomes && Object.values(outcomes).some((o) => o.status === 'failed') && (
        <Note>
          A click failed when replayed. It may depend on something this session already had
          open &mdash; reload the page and point at it again.
        </Note>
      )}
    </div>
  )
}

function Note({ children }: { children: React.ReactNode }) {
  return (
    <p className="flex items-start gap-1.5 rounded bg-warning/10 px-2 py-1 text-[11px] text-muted-foreground">
      <TriangleAlert className="mt-0.5 size-3 shrink-0 text-warning" />
      {children}
    </p>
  )
}

/**
 * What the picker's classifier decided this read yields, before any override.
 *
 * Mirrors `EXTRACTION_TYPE` in `fromPick.ts`, which is what actually applies it
 * — this only has to show the same default, so that pointing at an `<a>` reads
 * "Link" rather than "Text" with no sign the binding disagrees.
 */
function pickedAttribute(step: PreviewStep): string {
  const kind = (step.pick as { extractionType?: string } | undefined)?.extractionType
  if (kind === 'link' || kind === 'link_array') return 'href'
  if (kind === 'image' || kind === 'image_array') return 'src'
  return 'text'
}
