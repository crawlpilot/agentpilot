import { ChevronsDown, Eye, Keyboard, MousePointerClick, Timer, Trash2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Badge } from '@/components/ui/badge'
import { Reorderable } from '@/components/ui/reorderable'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { PickerControls } from './PickerControls'
import { moveItem } from '@/lib/recipe/document'
import type { PickerStatus } from '@/hooks/usePagePicker'
import type { OnError, Step, StepOp } from '@/lib/recipe/types'

interface Props {
  steps: Step[]
  onChange: (steps: Step[]) => void
  status: PickerStatus
  /** Pick a target element for a new step of this op. */
  onPickTarget: (op: StepOp) => void
  onCancel: () => void
  onRefine: (key: 'ArrowUp' | 'ArrowDown' | 'Enter') => void
}

/**
 * The subset of `StepOp` worth offering here.
 *
 * The contract has 28 ops; this is the handful that answer "the value is on
 * the page but not readable yet", which is the only question this step exists
 * for. Everything else -- tab management, drags, dialogs, navigation -- is
 * real, occasionally necessary, and belongs in the advanced editor rather than
 * in a list an author has to read past on the way to clicking one button.
 */
const ACTIONS: {
  op: StepOp
  label: string
  hint: string
  icon: typeof MousePointerClick
  needsTarget: boolean
  /** Free-text argument, if the op takes one. */
  arg?: { key: string; label: string; placeholder: string }
}[] = [
  {
    op: 'click',
    label: 'Click',
    hint: 'Open an accordion, dismiss a cookie banner, switch a tab.',
    icon: MousePointerClick,
    needsTarget: true,
  },
  {
    op: 'scroll_into_view',
    label: 'Scroll to element',
    hint: 'Bring lazy-loaded content into view so it renders.',
    icon: ChevronsDown,
    needsTarget: true,
  },
  {
    op: 'fill',
    label: 'Type into a field',
    hint: 'Enter a search term or a postcode that changes what the page shows.',
    icon: Keyboard,
    needsTarget: true,
    arg: { key: 'text', label: 'Text', placeholder: 'what to type' },
  },
  {
    op: 'wait_for_selector',
    label: 'Wait for element',
    hint: 'Wait until something appears, rather than guessing a duration.',
    icon: Eye,
    needsTarget: true,
  },
  {
    op: 'wait',
    label: 'Wait (fixed)',
    hint: 'A last resort — prefer waiting for an element.',
    icon: Timer,
    needsTarget: false,
    arg: { key: 'ms', label: 'Milliseconds', placeholder: '1000' },
  },
]

const ON_ERROR: OnError[] = ['fail', 'continue']

function describe(step: Step): string {
  const action = ACTIONS.find((a) => a.op === step.op)
  return action?.label ?? step.op
}

/**
 * Reveal steps: what has to happen to the page before the fields can be read.
 *
 * This is the extension's "Add Click Action", generalised to the ops a v2
 * recipe can express. It matters more here than it did there, because a v2
 * recipe is replayed unattended: a value behind an accordion is not missing,
 * it is one click away, and the difference between those two is the whole of
 * this step.
 *
 * Steps go into `global_setup` and run in order before any field is read.
 * `on_error` defaults to `continue` for exactly one reason: the most common
 * step here is dismissing a cookie banner that is not always there, and a
 * recipe that fails outright because a banner *did not* appear is worse than
 * one that shrugs and carries on.
 */
export function StepActions({ steps, onChange, status, onPickTarget, onCancel, onRefine }: Props) {
  function patch(index: number, next: Partial<Step>) {
    onChange(steps.map((s, i) => (i === index ? { ...s, ...next } : s)))
  }

  function setArg(index: number, key: string, value: string) {
    patch(index, { args: { ...(steps[index].args ?? {}), [key]: key === 'ms' ? Number(value) || 0 : value } })
  }

  return (
    <div className="flex flex-col gap-3 p-3">
      <div className="flex flex-col gap-1.5">
        <p className="text-[11px] leading-snug text-muted-foreground">
          Anything the page needs before the fields can be read. These run in order, once, before
          extraction.
        </p>
        <div className="flex flex-wrap gap-1">
          {ACTIONS.map((action) => (
            <Button
              key={action.op}
              size="sm"
              variant="outline"
              className="h-7"
              title={action.hint}
              disabled={status !== 'idle'}
              onClick={() => {
                if (action.needsTarget) {
                  onPickTarget(action.op)
                } else {
                  onChange([...steps, { op: action.op, on_error: 'continue', optional: true, args: {} }])
                }
              }}
            >
              <action.icon className="size-3.5" />
              {action.label}
            </Button>
          ))}
        </div>
      </div>

      {status !== 'idle' && (
        <div className="rounded-md border border-dashed border-border p-2.5">
          <PickerControls
            status={status}
            label="Pick target"
            onStart={() => {}}
            onCancel={onCancel}
            onRefine={onRefine}
          />
          <p className="pt-1.5 text-[11px] leading-snug text-muted-foreground">
            Click the element this step should act on, in the page on the left.
          </p>
        </div>
      )}

      {steps.length === 0 ? (
        <p className="rounded-md border border-dashed border-border p-3 text-[11px] text-muted-foreground">
          No steps. That is the right answer whenever every value is already visible on load.
        </p>
      ) : (
        <div>
          {steps.map((step, index) => {
            const action = ACTIONS.find((a) => a.op === step.op)
            return (
              <Reorderable
                key={index}
                index={index}
                count={steps.length}
                group="wizard:steps"
                label={`step ${index + 1} of ${steps.length}`}
                onMove={(from, to) => onChange(moveItem(steps, from, to))}
              >
                <div className="flex min-w-0 flex-col gap-1.5 py-1.5">
                  <div className="flex min-w-0 items-center gap-1.5">
                    <Badge variant="outline" className="shrink-0">
                      {index + 1}
                    </Badge>
                    <span className="shrink-0 text-xs font-medium">{describe(step)}</span>
                    {step.target?.selector && (
                      <code
                        className="min-w-0 flex-1 truncate rounded bg-muted px-1.5 py-0.5 font-mono text-[10px]"
                        title={step.target.selector}
                      >
                        {step.target.selector}
                      </code>
                    )}
                    <Button
                      size="sm"
                      variant="ghost"
                      className="h-6 shrink-0 px-1.5"
                      aria-label={`Remove step ${index + 1}`}
                      onClick={() => onChange(steps.filter((_, i) => i !== index))}
                    >
                      <Trash2 className="size-3" />
                    </Button>
                  </div>

                  <div className="flex items-center gap-1.5 pl-7">
                    {action?.arg && (
                      <Input
                        className="h-6 flex-1 text-[11px]"
                        placeholder={action.arg.placeholder}
                        value={String(step.args?.[action.arg.key] ?? '')}
                        onChange={(e) => setArg(index, action.arg!.key, e.target.value)}
                      />
                    )}
                    <Select
                      value={step.on_error ?? 'continue'}
                      onValueChange={(v) => patch(index, { on_error: v as OnError })}
                    >
                      <SelectTrigger className="h-6 w-28 text-[11px]">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        {ON_ERROR.map((e) => (
                          <SelectItem key={e} value={e}>
                            on error: {e}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </div>
                </div>
              </Reorderable>
            )
          })}
        </div>
      )}
    </div>
  )
}
