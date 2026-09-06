import { useState } from 'react'
import {
  ChevronDown,
  ChevronRight,
  ChevronsDown,
  Eye,
  Keyboard,
  List,
  MousePointer2,
  MousePointerClick,
  RotateCcw,
  Sparkles,
  Timer,
  Trash2,
} from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Badge } from '@/components/ui/badge'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Reorderable } from '@/components/ui/reorderable'
import { CandidateChain } from './CandidateChain'
import { PickerControls } from './PickerControls'
import { describeTypeSpec, moveItem } from '@/lib/recipe/document'
import { readAttribute, setReadAttribute, type FieldDraft, type WorkItem } from '@/lib/recipe/fromPick'
import type { PickerStatus } from '@/hooks/usePagePicker'
import type { PickerMode } from '@/lib/picker/protocol'
import type { Candidate, OnError, Step, StepOp, ValueType } from '@/lib/recipe/types'
import { cn } from '@/lib/utils'

interface Props {
  items: WorkItem[]
  onChange: (items: WorkItem[]) => void
  pickMode: Exclude<PickerMode, 'single'>
  onPickModeChange: (mode: Exclude<PickerMode, 'single'>) => void
  status: PickerStatus
  onPickField: () => void
  onPickAction: (op: StepOp) => void
  onAddPlainAction: (op: StepOp) => void
  onCancel: () => void
  onRefine: (key: 'ArrowUp' | 'ArrowDown' | 'Enter') => void
  onTestSelector?: (selector: string) => Promise<number>
  onFindInJson?: (draft: FieldDraft, index: number) => void
  jsonProbeReady: boolean
}

const PICK_MODES: { id: Exclude<PickerMode, 'single'>; label: string; icon: typeof List }[] = [
  { id: 'list', label: 'Repeating list', icon: List },
  { id: 'detail', label: 'Single field', icon: MousePointer2 },
]

const ACTIONS: {
  op: StepOp
  label: string
  icon: typeof MousePointerClick
  needsTarget: boolean
  arg?: { key: string; placeholder: string }
}[] = [
  { op: 'click', label: 'Click', icon: MousePointerClick, needsTarget: true },
  { op: 'scroll_into_view', label: 'Scroll to', icon: ChevronsDown, needsTarget: true },
  { op: 'fill', label: 'Type', icon: Keyboard, needsTarget: true, arg: { key: 'text', placeholder: 'text to type' } },
  { op: 'wait_for_selector', label: 'Wait for', icon: Eye, needsTarget: true },
  { op: 'wait', label: 'Wait', icon: Timer, needsTarget: false, arg: { key: 'ms', placeholder: 'ms' } },
]

const VALUE_TYPES: ValueType[] = [
  'string', 'text', 'number', 'integer', 'float', 'price', 'boolean', 'url', 'date', 'datetime', 'json',
]

const READ_ATTRIBUTES: { value: string; label: string; hint: string }[] = [
  { value: 'text', label: 'Text', hint: 'The element’s text, excluding script/style. Includes text present but not painted.' },
  { value: 'visible_text', label: 'Visible text', hint: 'Only what is rendered — excludes collapsed content.' },
  { value: 'href', label: 'Link (href)', hint: 'The link target.' },
  { value: 'src', label: 'Image (src)', hint: 'The image source URL.' },
  { value: 'value', label: 'Form value', hint: 'The current value of an input, select or textarea.' },
  { value: 'html', label: 'HTML', hint: 'Outer HTML — only when the markup itself is the data.' },
  { value: 'title', label: 'title', hint: 'Often the full text when the visible label is truncated.' },
  { value: 'alt', label: 'alt', hint: 'An image’s alt text.' },
  { value: 'content', label: 'content', hint: 'As used by meta tags.' },
  { value: 'datetime', label: 'datetime', hint: 'A <time> element’s machine-readable timestamp.' },
]

/**
 * Fields and reveal actions, in one ordered list.
 *
 * They are together because on a real page they interleave. A value behind a
 * drawer needs the click that opens it, and that click is only meaningful
 * *before that particular read* -- so expressing the sequence in two separate
 * screens means jumping between them to say one thing. The extension keeps
 * "Pick Data Fields" and "Add Click Action" in a single list for exactly this
 * reason, and the ordering the list captures is real: `itemsToRecipe` compiles
 * it into `global_setup` plus the `field_groups` those actions unlock.
 */
export function StepExtract({
  items,
  onChange,
  pickMode,
  onPickModeChange,
  status,
  onPickField,
  onPickAction,
  onAddPlainAction,
  onCancel,
  onRefine,
  onTestSelector,
  onFindInJson,
  jsonProbeReady,
}: Props) {
  const [expanded, setExpanded] = useState<string | null>(null)
  const picking = status !== 'idle'

  const fieldCount = items.filter((i) => i.kind === 'field').length
  const actionCount = items.filter((i) => i.kind === 'action').length

  function patchField(id: string, next: Partial<FieldDraft>) {
    onChange(
      items.map((item) =>
        item.kind === 'field' && item.id === id ? { ...item, draft: { ...item.draft, ...next } } : item,
      ),
    )
  }

  function patchStep(id: string, next: Record<string, unknown>) {
    onChange(
      items.map((item) =>
        item.kind === 'action' && item.id === id ? { ...item, step: { ...item.step, ...next } } : item,
      ),
    )
  }

  const fieldIndexOf = (id: string) =>
    items.filter((i) => i.kind === 'field').findIndex((i) => i.id === id)

  return (
    <div className="flex flex-col gap-3 p-3">
      {/* --- what to add --- */}
      <div className="flex flex-col gap-2 rounded-md border border-border p-2.5">
        <div className="flex items-center gap-1">
          {PICK_MODES.map((m) => (
            <Button
              key={m.id}
              size="sm"
              variant={pickMode === m.id ? 'default' : 'outline'}
              className="h-7 flex-1"
              disabled={picking}
              onClick={() => onPickModeChange(m.id)}
            >
              <m.icon className="size-3.5" />
              {m.label}
            </Button>
          ))}
        </div>

        <PickerControls
          status={status}
          label={pickMode === 'list' ? 'Pick a list item' : 'Pick a field'}
          onStart={onPickField}
          onCancel={onCancel}
          onRefine={onRefine}
        />

        <div className="flex flex-wrap items-center gap-1 border-t border-border pt-2">
          <span className="mr-1 text-[10px] uppercase tracking-wide text-muted-foreground">
            Reveal
          </span>
          {ACTIONS.map((a) => (
            <Button
              key={a.op}
              size="sm"
              variant="ghost"
              className="h-6 px-1.5 text-[11px]"
              disabled={picking}
              onClick={() => (a.needsTarget ? onPickAction(a.op) : onAddPlainAction(a.op))}
            >
              <a.icon className="size-3" />
              {a.label}
            </Button>
          ))}
          <Button
            size="sm"
            variant="ghost"
            className="h-6 px-1.5 text-[11px]"
            disabled={picking}
            title="Everything below starts from a fresh page load. Use this when two reveals conflict — two drawers that close each other cannot both be open."
            onClick={() => onChange([...items, { kind: 'reset', id: `reset-${Date.now()}` }])}
          >
            <RotateCcw className="size-3" />
            Reload
          </Button>
        </div>

        {picking && (
          <p className="text-[11px] leading-snug text-muted-foreground">
            Click the element in the page on the left. Use Wider / Narrower to adjust first.
          </p>
        )}
      </div>

      {/* --- the list --- */}
      {items.length === 0 ? (
        <p className="rounded-md border border-dashed border-border p-3 text-[11px] leading-snug text-muted-foreground">
          Nothing yet. Pick a field to read, or add a reveal action if what you want is behind a
          click. Order matters &mdash; an action applies to every field below it.
        </p>
      ) : (
        <>
          <div className="flex items-center gap-1.5">
            <Badge variant="outline">{fieldCount} fields</Badge>
            {actionCount > 0 && <Badge variant="outline">{actionCount} actions</Badge>}
            <Button
              size="sm"
              variant="ghost"
              className="ml-auto h-6 px-1.5 text-[11px]"
              onClick={() => onChange([])}
            >
              Clear all
            </Button>
          </div>

          <div>
            {items.map((item, index) => (
              <Reorderable
                key={item.id}
                index={index}
                count={items.length}
                group="wizard:extract"
                label={`item ${index + 1} of ${items.length}`}
                onMove={(from, to) => onChange(moveItem(items, from, to))}
              >
                {item.kind === 'reset' ? (
                  <div className="flex min-w-0 items-center gap-2 py-1.5">
                    <RotateCcw className="size-3.5 shrink-0 text-muted-foreground" />
                    <span className="min-w-0 flex-1 text-[11px] text-muted-foreground">
                      Reload the page &mdash; everything below starts fresh
                    </span>
                    <Button
                      size="sm"
                      variant="ghost"
                      className="h-6 shrink-0 px-1.5"
                      aria-label="Remove reload marker"
                      onClick={() => onChange(items.filter((i) => i.id !== item.id))}
                    >
                      <Trash2 className="size-3" />
                    </Button>
                  </div>
                ) : item.kind === 'action' ? (
                  <ActionRow
                    step={item.step}
                    onPatch={(next) => patchStep(item.id, next)}
                    onRemove={() => onChange(items.filter((i) => i.id !== item.id))}
                  />
                ) : (
                  <FieldRow
                    draft={item.draft}
                    open={expanded === item.id}
                    onToggle={() => setExpanded(expanded === item.id ? null : item.id)}
                    onPatch={(next) => patchField(item.id, next)}
                    onRemove={() => onChange(items.filter((i) => i.id !== item.id))}
                    onTestSelector={onTestSelector}
                    onFindInJson={
                      onFindInJson ? () => onFindInJson(item.draft, fieldIndexOf(item.id)) : undefined
                    }
                    jsonProbeReady={jsonProbeReady}
                    group={`chain:${item.id}`}
                  />
                )}
              </Reorderable>
            ))}
          </div>
        </>
      )}
    </div>
  )
}

function ActionRow({
  step,
  onPatch,
  onRemove,
}: {
  step: Step
  onPatch: (next: Record<string, unknown>) => void
  onRemove: () => void
}) {
  const action = ACTIONS.find((a) => a.op === step.op)
  const Icon = action?.icon ?? MousePointerClick
  return (
    <div className="flex min-w-0 flex-col gap-1 py-1.5">
      <div className="flex min-w-0 items-center gap-1.5">
        <Icon className="size-3.5 shrink-0 text-warning" />
        <span className="shrink-0 text-xs font-medium">{action?.label ?? step.op}</span>
        {step.target?.selector && (
          <code
            className="min-w-0 flex-1 truncate rounded bg-muted px-1.5 py-0.5 font-mono text-[10px]"
            title={step.target.selector}
          >
            {step.target.selector}
          </code>
        )}
        <Button size="sm" variant="ghost" className="h-6 shrink-0 px-1.5" aria-label="Remove action" onClick={onRemove}>
          <Trash2 className="size-3" />
        </Button>
      </div>
      <div className="flex items-center gap-1.5 pl-5">
        {action?.arg && (
          <Input
            className="h-6 flex-1 text-[11px]"
            placeholder={action.arg.placeholder}
            value={String(step.args?.[action.arg.key] ?? '')}
            onChange={(e) =>
              onPatch({
                args: {
                  ...(step.args ?? {}),
                  [action.arg!.key]:
                    action.arg!.key === 'ms' ? Number(e.target.value) || 0 : e.target.value,
                },
              })
            }
          />
        )}
        <Select value={step.on_error ?? 'continue'} onValueChange={(v) => onPatch({ on_error: v as OnError })}>
          <SelectTrigger className="h-6 w-32 text-[11px]">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="continue">on error: continue</SelectItem>
            <SelectItem value="fail">on error: fail</SelectItem>
          </SelectContent>
        </Select>
      </div>
    </div>
  )
}

function FieldRow({
  draft,
  open,
  onToggle,
  onPatch,
  onRemove,
  onTestSelector,
  onFindInJson,
  jsonProbeReady,
  group,
}: {
  draft: FieldDraft
  open: boolean
  onToggle: () => void
  onPatch: (next: Partial<FieldDraft>) => void
  onRemove: () => void
  onTestSelector?: (selector: string) => Promise<number>
  onFindInJson?: () => void
  jsonProbeReady: boolean
  group: string
}) {
  const inner = draft.spec.type.kind === 'list' ? draft.spec.type.items : draft.spec.type
  const structured = draft.candidates.some((c) =>
    ['json_ld', 'hydration', 'meta'].includes(c.locator.kind),
  )
  const attribute = readAttribute(draft.candidates) ?? 'text'

  function setCandidates(candidates: Candidate[]) {
    onPatch({ candidates })
  }

  function setValueType(value_type: ValueType) {
    const type =
      draft.spec.type.kind === 'list'
        ? { ...draft.spec.type, items: { kind: 'scalar' as const, value_type } }
        : { ...draft.spec.type, value_type }
    onPatch({ spec: { ...draft.spec, type } })
  }

  return (
    <div className="flex min-w-0 flex-col py-1">
      <div className="flex min-w-0 items-center gap-1.5">
        <button type="button" className="shrink-0 text-muted-foreground" onClick={onToggle}
          aria-label={open ? 'Collapse' : 'Expand'}>
          {open ? <ChevronDown className="size-4" /> : <ChevronRight className="size-4" />}
        </button>
        <Input
          value={draft.name}
          onChange={(e) => onPatch({ name: e.target.value })}
          className="h-7 w-32 shrink-0 font-mono text-xs"
          placeholder="field_name"
          title="The output attribute name — the key this value appears under in the extracted data"
          aria-label="Output attribute name"
        />
        <Badge variant="outline" className="shrink-0" title={describeTypeSpec(draft.spec.type)}>
          {describeTypeSpec(draft.spec.type)}
        </Badge>
        {draft.preview && (
          <span className="min-w-0 flex-1 truncate text-[11px] text-muted-foreground" title={draft.preview}>
            {draft.preview}
          </span>
        )}
        <Button size="sm" variant="ghost" className="h-6 shrink-0 px-1.5" aria-label={`Remove ${draft.name}`}
          onClick={onRemove}>
          <Trash2 className="size-3" />
        </Button>
      </div>

      {open && (
        <div className="flex flex-col gap-2 pl-5 pt-2">
          <div className="flex items-center gap-1.5">
            <Select value={attribute} onValueChange={(v) => setCandidates(setReadAttribute(draft.candidates, v))}>
              <SelectTrigger className="h-7 flex-1 text-xs">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {READ_ATTRIBUTES.map((a) => (
                  <SelectItem key={a.value} value={a.value}>reads: {a.label}</SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Select value={inner?.value_type ?? 'string'} onValueChange={(v) => setValueType(v as ValueType)}>
              <SelectTrigger className="h-7 w-24 text-xs">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {VALUE_TYPES.map((t) => (
                  <SelectItem key={t} value={t}>{t}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <p className="text-[10px] leading-snug text-muted-foreground">
            {READ_ATTRIBUTES.find((a) => a.value === attribute)?.hint}
          </p>

          {!structured && jsonProbeReady && onFindInJson && (
            <Button size="sm" variant="outline" className={cn('h-6 w-fit px-1.5 text-[11px]')}
              title="Look for this value in the page's JSON-LD, hydration state or meta tags"
              onClick={onFindInJson}>
              <Sparkles className="size-3" />
              Find in page JSON
            </Button>
          )}

          <div>
            <p className="pb-1 text-[10px] uppercase tracking-wide text-muted-foreground">
              Fallback chain &mdash; tried in order until one resolves
            </p>
            <CandidateChain
              group={group}
              candidates={draft.candidates}
              onChange={setCandidates}
              onTest={onTestSelector}
            />
          </div>
        </div>
      )}
    </div>
  )
}
