import { useState } from 'react'
import { ChevronDown, ChevronRight, Trash2 } from 'lucide-react'
import { Input } from '@/components/ui/input'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { LocatorEditor } from '@/components/app/studio/LocatorEditor'
import { PredicateList } from '@/components/app/studio/PredicateEditor'
import { OPS_NEEDING_TARGET, STEP_OPS, describeLocator, newLocator } from '@/lib/recipe/document'
import type { OnError, Step, StepOp } from '@/lib/recipe/types'

// The args each op actually reads, from `build_action` in
// agentpilot/recipe/v2/steps.py. Anything else in `args` is dead weight, so
// the editor only offers these.
function StepArgs({ step, onChange }: { step: Step; onChange: (s: Step) => void }) {
  const args = step.args ?? {}
  const setArg = (key: string, value: unknown) => onChange({ ...step, args: { ...args, [key]: value } })
  const text = (key: string) => (typeof args[key] === 'string' ? (args[key] as string) : '')

  switch (step.op) {
    case 'navigate':
      return (
        <>
          <Input
            className="h-7 flex-1 font-mono text-xs"
            placeholder="https://… or {{meta.url}}"
            value={text('url')}
            onChange={(e) => setArg('url', e.target.value)}
          />
          <Select value={text('wait_until') || 'load'} onValueChange={(v) => setArg('wait_until', v)}>
            <SelectTrigger className="h-7 w-40 text-xs">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="load">load</SelectItem>
              <SelectItem value="domcontentloaded">domcontentloaded</SelectItem>
              <SelectItem value="networkidle">networkidle</SelectItem>
            </SelectContent>
          </Select>
        </>
      )
    case 'fill':
      return (
        <>
          <Input
            className="h-7 flex-1 text-xs"
            placeholder="text to type -- {{meta.*}} is substituted"
            value={text('text')}
            onChange={(e) => setArg('text', e.target.value)}
          />
          <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
            <input
              type="checkbox"
              checked={args.clear !== false}
              onChange={(e) => setArg('clear', e.target.checked)}
            />
            clear first
          </label>
        </>
      )
    case 'press':
      return (
        <Input
          className="h-7 w-32 font-mono text-xs"
          placeholder="Enter"
          value={text('key')}
          onChange={(e) => setArg('key', e.target.value)}
        />
      )
    case 'send_keys':
      return (
        <Input
          className="h-7 flex-1 font-mono text-xs"
          placeholder="keys"
          value={text('keys')}
          onChange={(e) => setArg('keys', e.target.value)}
        />
      )
    case 'select_option':
      return (
        <Input
          className="h-7 flex-1 font-mono text-xs"
          placeholder="comma-separated values"
          value={Array.isArray(args.values) ? (args.values as string[]).join(', ') : ''}
          onChange={(e) => setArg('values', e.target.value.split(',').map((s) => s.trim()).filter(Boolean))}
        />
      )
    case 'scroll':
    case 'swipe':
      return (
        <>
          <Select value={text('direction') || 'down'} onValueChange={(v) => setArg('direction', v)}>
            <SelectTrigger className="h-7 w-24 text-xs">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {['up', 'down', 'left', 'right'].map((d) => (
                <SelectItem key={d} value={d}>
                  {d}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Input
            type="number"
            className="h-7 w-24 text-xs"
            placeholder={step.op === 'scroll' ? 'pages' : 'distance'}
            value={(args[step.op === 'scroll' ? 'pages' : 'distance'] as number) ?? ''}
            onChange={(e) =>
              setArg(step.op === 'scroll' ? 'pages' : 'distance', e.target.value ? Number(e.target.value) : undefined)
            }
          />
        </>
      )
    case 'wait':
      return (
        <Input
          type="number"
          className="h-7 w-28 text-xs"
          placeholder="ms"
          value={(args.ms as number) ?? ''}
          onChange={(e) => setArg('ms', e.target.value ? Number(e.target.value) : undefined)}
        />
      )
    case 'wait_for_selector':
      return (
        <Select value={text('state') || 'visible'} onValueChange={(v) => setArg('state', v)}>
          <SelectTrigger className="h-7 w-32 text-xs">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {['attached', 'detached', 'visible', 'hidden'].map((s) => (
              <SelectItem key={s} value={s}>
                {s}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      )
    case 'wait_for_text':
    case 'find_text':
      return (
        <Input
          className="h-7 flex-1 text-xs"
          placeholder="text"
          value={text('text')}
          onChange={(e) => setArg('text', e.target.value)}
        />
      )
    case 'wait_for_url':
      return (
        <Input
          className="h-7 flex-1 font-mono text-xs"
          placeholder="url or pattern"
          value={text('url')}
          onChange={(e) => setArg('url', e.target.value)}
        />
      )
    case 'wait_for_load':
      return (
        <Select value={text('state') || 'load'} onValueChange={(v) => setArg('state', v)}>
          <SelectTrigger className="h-7 w-44 text-xs">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="load">load</SelectItem>
            <SelectItem value="domcontentloaded">domcontentloaded</SelectItem>
            <SelectItem value="networkidle">networkidle</SelectItem>
          </SelectContent>
        </Select>
      )
    case 'dialog_accept':
      return (
        <Input
          className="h-7 flex-1 text-xs"
          placeholder="prompt text (optional)"
          value={text('prompt_text')}
          onChange={(e) => setArg('prompt_text', e.target.value || undefined)}
        />
      )
    case 'new_tab':
      return (
        <Input
          className="h-7 flex-1 font-mono text-xs"
          placeholder="url (optional)"
          value={text('url')}
          onChange={(e) => setArg('url', e.target.value || undefined)}
        />
      )
    case 'switch_tab':
    case 'close_tab':
      return (
        <Input
          className="h-7 w-44 font-mono text-xs"
          placeholder="page_id"
          value={text('page_id')}
          onChange={(e) => setArg('page_id', e.target.value)}
        />
      )
    default:
      return null
  }
}

const ON_ERROR: OnError[] = ['fail', 'continue', 'skip_group']

interface Props {
  step: Step
  onChange: (next: Step) => void
  onRemove: () => void
  /** `skip_group` is meaningless on a global_setup step -- there is no group. */
  allowSkipGroup?: boolean
}

export function StepRow({ step, onChange, onRemove, allowSkipGroup = true }: Props) {
  const [expanded, setExpanded] = useState(false)
  const needsTarget = OPS_NEEDING_TARGET.has(step.op)
  const guards = step.when ?? []

  return (
    <div className="rounded-md border border-border p-1.5">
      <div className="flex flex-wrap items-center gap-1.5">
        <Button size="icon" variant="ghost" className="size-6" onClick={() => setExpanded(!expanded)}>
          {expanded ? <ChevronDown className="size-3.5" /> : <ChevronRight className="size-3.5" />}
        </Button>
        <Select
          value={step.op}
          onValueChange={(op) => {
            const next: Step = { ...step, op: op as StepOp, args: {} }
            if (OPS_NEEDING_TARGET.has(op as StepOp) && !next.target) next.target = newLocator('css')
            onChange(next)
          }}
        >
          <SelectTrigger className="h-7 w-44 text-xs">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {STEP_OPS.map((op) => (
              <SelectItem key={op} value={op}>
                {op}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>

        <StepArgs step={step} onChange={onChange} />

        {!expanded && step.target && (
          <span className="truncate font-mono text-[11px] text-muted-foreground">
            {describeLocator(step.target)}
          </span>
        )}
        {step.on_error && step.on_error !== 'fail' && <Badge variant="outline">{step.on_error}</Badge>}
        {guards.length > 0 && <Badge variant="outline">{guards.length} guard{guards.length > 1 ? 's' : ''}</Badge>}

        <Button size="icon" variant="ghost" className="ml-auto size-6" onClick={onRemove}>
          <Trash2 className="size-3.5" />
        </Button>
      </div>

      {expanded && (
        <div className="mt-2 flex flex-col gap-2.5 border-t border-border/60 pt-2 pl-7">
          {(needsTarget || step.target) && (
            <div className="flex flex-col gap-1">
              <span className="text-[11px] font-medium text-muted-foreground">Target</span>
              <LocatorEditor
                locator={step.target ?? newLocator('css')}
                onChange={(target) => onChange({ ...step, target })}
                actionTarget
              />
            </div>
          )}

          <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
            <label className="flex items-center gap-1.5">
              timeout
              <Input
                type="number"
                className="h-7 w-24 text-xs"
                placeholder="default"
                value={step.timeout_ms ?? ''}
                onChange={(e) => onChange({ ...step, timeout_ms: e.target.value ? Number(e.target.value) : null })}
              />
              ms
            </label>
            <label className="flex items-center gap-1.5">
              on error
              <Select
                value={step.on_error ?? 'fail'}
                onValueChange={(v) => onChange({ ...step, on_error: v as OnError })}
              >
                <SelectTrigger className="h-7 w-32 text-xs">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {ON_ERROR.filter((v) => allowSkipGroup || v !== 'skip_group').map((v) => (
                    <SelectItem key={v} value={v}>
                      {v}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </label>
            <label className="flex items-center gap-1.5">
              retries
              <Input
                type="number"
                min={0}
                className="h-7 w-16 text-xs"
                value={step.retry?.attempts ?? ''}
                onChange={(e) =>
                  onChange({
                    ...step,
                    retry: e.target.value ? { attempts: Number(e.target.value), backoff_ms: step.retry?.backoff_ms ?? 250 } : null,
                  })
                }
              />
            </label>
            <Input
              className="h-7 w-48 text-xs"
              placeholder="label (shown in the trace)"
              value={step.label ?? ''}
              onChange={(e) => onChange({ ...step, label: e.target.value || null })}
            />
          </div>

          <div className="flex flex-col gap-1">
            <span className="text-[11px] font-medium text-muted-foreground">Run only when</span>
            <PredicateList
              predicates={guards}
              onChange={(when) => onChange({ ...step, when })}
              label="add guard"
              hint="unguarded -- runs every time"
            />
          </div>
        </div>
      )}
    </div>
  )
}
