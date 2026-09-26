import { useState } from 'react'
import {
  ArrowLeft,
  Check,
  Eye,
  Frame,
  HelpCircle,
  ListOrdered,
  SkipForward,
} from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Input } from '@/components/ui/input'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { PickerControls } from '@/components/app/wizard/PickerControls'
import { RouteEditor } from './RouteEditor'
import { READ_ATTRIBUTES, attributeHint } from '@/lib/recipe/attributes'
import { readAttribute } from '@/lib/recipe/fromPick'
import { splitAtRead, type AskMode, type AskState } from '@/lib/recipe/assistRoute'
import type { PreviewStep } from '@/lib/picker/preview'
import type { PendingAsk } from '@/lib/api/types'
import type { usePagePicker } from '@/hooks/usePagePicker'

export type ScopeShape = 'one' | 'values' | 'map' | 'rows'

/**
 * One field the build could not settle, and the one way it is being answered.
 *
 * **One mechanism at a time.** This row used to render all six at once — point
 * at the value, point at the section with four shape buttons, a recorder with
 * seven, a hint box, keep-this-value and skip — stacked under as many as five
 * explanation blocks, in a 22rem column. `AskMode` is what replaced that: the
 * row asks how you want to answer, then shows only that.
 *
 * **Answered is a thing you do.** The old row was a binary on a derived value —
 * `answer ? summary : controls`, where `answer` came from the panel's
 * work-in-progress maps. So a route gaining its first entry flipped the row to
 * a summary and unmounted the editor being used to build it; and because the
 * recorder it unmounted held a page-wide lock, that then disabled every other
 * ask on the screen. `mode === 'done'` is set by committing and nothing else.
 *
 * Picking itself stays in the panel, which owns the session and the conversions
 * (`detailPickToDraft`, `scopeFromPick`, the read-back). This renders.
 */
export function AskRow({
  ask,
  state,
  selected,
  canPick,
  picker,
  onSelect,
  onState,
  onDone,
  onPickValue,
  onPickScope,
  onAttribute,
}: {
  ask: PendingAsk
  state: AskState
  selected: boolean
  canPick: boolean
  picker: ReturnType<typeof usePagePicker>
  onSelect: () => void
  onState: (next: Partial<AskState>) => void
  /** Commit this ask, and move the panel on to the next unanswered one. */
  onDone: (next: Partial<AskState>) => void
  onPickValue: () => void
  onPickScope: (shape: ScopeShape) => void
  onAttribute: (attribute: string) => void
}) {
  const [hint, setHint] = useState('')
  const answered = state.mode === 'done'

  if (!selected) {
    return (
      <button
        type="button"
        onClick={onSelect}
        className="flex w-full items-center gap-2 rounded-md border border-border px-3 py-2 text-left hover:border-accent/60"
      >
        <span
          className={`size-1.5 shrink-0 rounded-full ${
            answered ? 'bg-success' : 'bg-muted-foreground/40'
          }`}
        />
        <span className="min-w-0 flex-1 truncate font-mono text-xs">{ask.field}</span>
        {answered ? (
          <Check className="size-3.5 shrink-0 text-success" />
        ) : (
          <Badge variant={ask.kind === 'unresolved' ? 'outline' : 'warning'} className="shrink-0">
            {KIND_LABEL[ask.kind]}
          </Badge>
        )}
      </button>
    )
  }

  return (
    <div className="flex flex-col gap-2 rounded-md border border-accent p-3">
      <div className="flex items-center gap-2">
        <span className="font-mono text-xs font-medium">{ask.field}</span>
        <Badge variant={ask.kind === 'unresolved' ? 'outline' : 'warning'}>
          {KIND_LABEL[ask.kind]}
        </Badge>
        {answered && (
          <Badge variant="success" className="ml-auto">
            answered
          </Badge>
        )}
      </div>

      <p className="text-xs text-muted-foreground">{ask.reason}</p>

      <Why ask={ask} />

      {answered ? (
        <Summary
          state={state}
          onAttribute={onAttribute}
          onClear={() => onState(CLEARED)}
        />
      ) : state.mode === 'choosing' ? (
        <Choosing
          ask={ask}
          canPick={canPick}
          onMode={(mode) => onState({ mode })}
          onAccept={() => onDone({ answer: { field: ask.field, action: 'accept' } })}
          onSkip={() => onDone({ answer: { field: ask.field, action: 'skip' } })}
        />
      ) : (
        <div className="flex flex-col gap-2">
          <Button
            size="sm"
            variant="ghost"
            className="h-6 self-start px-2 text-[11px] text-muted-foreground"
            onClick={() => onState(CLEARED)}
          >
            <ArrowLeft className="size-3" />
            Answer a different way
          </Button>

          {state.mode === 'pick' && (
            <PickerControls
              status={picker.status}
              label={state.pick ? 'Point at a different element' : 'Point at the value'}
              disabled={!canPick}
              onStart={onPickValue}
              onCancel={picker.cancel}
              onRefine={picker.refine}
            />
          )}

          {state.mode === 'pick' && state.pick && (
            <Reads
              attribute={readAttribute(state.pick.candidates) ?? 'text'}
              preview={state.pick.preview}
              onAttribute={onAttribute}
            />
          )}

          {state.mode === 'scope' && (
            <Scope canPick={canPick} scope={state.scope} onScope={onPickScope} />
          )}

          {state.mode === 'route' && (
            <RouteEditor
              picker={picker}
              route={state.route}
              disabled={!canPick}
              onChange={(route) => onState({ route })}
            />
          )}

          {state.mode === 'describe' && (
            <div className="flex items-center gap-1.5">
              <HelpCircle className="size-3.5 shrink-0 text-muted-foreground" />
              <Input
                autoFocus
                className="h-7 text-xs"
                placeholder="where is it? — 'inside the Details accordion'"
                value={hint}
                onChange={(e) => setHint(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && hint.trim()) {
                    onDone({ answer: { field: ask.field, action: 'describe', hint: hint.trim() } })
                  }
                }}
              />
            </div>
          )}

          {readyToCommit(state) && (
            <Button size="sm" className="self-start" onClick={() => onDone({})}>
              <Check className="size-3.5" />
              Use this
            </Button>
          )}
        </div>
      )}
    </div>
  )
}

/**
 * Reset every per-field field.
 *
 * Spelled out rather than partial, because the partial version is the bug:
 * Change used to prune three of the panel's four per-field maps and leave the
 * route behind, so the answer was immediately re-derived from it and the row
 * snapped shut again with no way back.
 */
const CLEARED: Partial<AskState> = {
  mode: 'choosing',
  route: [],
  pick: undefined,
  scope: undefined,
  answer: undefined,
}

const KIND_LABEL: Record<PendingAsk['kind'], string> = {
  unresolved: 'not found',
  rejected: 'looked wrong',
  absent: 'not on this page',
}

/**
 * Whether the chosen mechanism has produced something worth sending.
 *
 * `describe` commits on Enter, `accept`/`skip` on click, and `scope` as soon as
 * the region resolves — so only the two that accumulate state need an explicit
 * Use this.
 */
function readyToCommit(state: AskState): boolean {
  if (state.mode === 'pick') return Boolean(state.pick)
  if (state.mode === 'route') {
    const { read, before } = splitAtRead(state.route)
    return Boolean(read) || before.length > 0
  }
  return false
}

/**
 * Everything diagnostic, behind one disclosure.
 *
 * All four are worth having and none is needed until somebody is confused.
 * Stacked open they were most of the row's height, which is what made the
 * mechanism underneath them hard to find at all.
 */
function Why({ ask }: { ask: PendingAsk }) {
  const failed = ask.step_trace.filter((s) => s.status === 'failed')
  const has =
    (ask.kind === 'rejected' && ask.value) || ask.kind === 'absent' || failed.length || ask.tried
  if (!has) return null
  return (
    <details className="rounded bg-muted/50 px-2 py-1 text-[11px] text-muted-foreground">
      <summary className="cursor-pointer select-none">why this is being asked</summary>
      <div className="mt-1 flex flex-col gap-1">
        {ask.kind === 'rejected' && ask.value && (
          <span>
            It read: &ldquo;{ask.value.length > 300 ? `${ask.value.slice(0, 300)}…` : ask.value}
            &rdquo;
          </span>
        )}
        {ask.kind === 'absent' && (
          <span>
            It stopped looking rather than keep returning a different wrong element each time. If
            this really is on the page, point at the section it&rsquo;s in &mdash; otherwise drop
            it.
          </span>
        )}
        {failed.length > 0 && (
          <span>
            {failed.length === 1 ? 'A step' : `${failed.length} steps`} before this field did not
            run &mdash; it may be hidden rather than missing.
          </span>
        )}
        {ask.tried && (
          <pre className="overflow-x-auto whitespace-pre-wrap font-mono text-[10px]">
            {ask.tried}
          </pre>
        )}
      </div>
    </details>
  )
}

function Choosing({
  ask,
  canPick,
  onMode,
  onAccept,
  onSkip,
}: {
  ask: PendingAsk
  canPick: boolean
  onMode: (mode: AskMode) => void
  onAccept: () => void
  onSkip: () => void
}) {
  return (
    <div className="flex flex-col gap-1.5">
      <span className="text-[11px] font-medium">How do you want to answer this?</span>
      <div className="flex flex-wrap gap-1">
        <Button
          size="sm" variant="outline" className="h-6 px-2 text-[11px]"
          disabled={!canPick} onClick={() => onMode('pick')}
          title="It is visible on the page right now — just point at it"
        >
          <Eye className="size-3" />
          Point at it
        </Button>
        <Button
          size="sm" variant="outline" className="h-6 px-2 text-[11px]"
          disabled={!canPick} onClick={() => onMode('route')}
          title="It takes a click or two to reach — point at each one, then at the value"
        >
          <ListOrdered className="size-3" />
          Click to reach it
        </Button>
        <Button
          size="sm" variant="outline" className="h-6 px-2 text-[11px]"
          disabled={!canPick} onClick={() => onMode('scope')}
          title="Point at the block it is inside and let the model find it in there"
        >
          <Frame className="size-3" />
          Point at the section
        </Button>
        <Button
          size="sm" variant="outline" className="h-6 px-2 text-[11px]"
          onClick={() => onMode('describe')}
        >
          <HelpCircle className="size-3" />
          Describe it
        </Button>
      </div>
      <div className="flex flex-wrap gap-1">
        {ask.kind === 'rejected' && ask.value && (
          <Button size="sm" variant="ghost" className="h-6 px-2 text-[11px]" onClick={onAccept}>
            <Check className="size-3" />
            Keep what it read
          </Button>
        )}
        <Button
          size="sm" variant="ghost" className="h-6 px-2 text-[11px] text-muted-foreground"
          onClick={onSkip}
        >
          <SkipForward className="size-3" />
          Don&rsquo;t collect this
        </Button>
      </div>
    </div>
  )
}

function Scope({
  canPick,
  scope,
  onScope,
}: {
  canPick: boolean
  scope: AskState['scope']
  onScope: (shape: ScopeShape) => void
}) {
  return (
    <div className="flex flex-col gap-1">
      <p className="text-[11px] text-muted-foreground">
        Use <b>↑</b> to grow the selection past the heading until it covers the whole block. A
        heading is not the value &mdash; select the region and the model finds the value inside it.
      </p>
      <div className="flex flex-wrap gap-1">
        {(['one', 'values', 'map', 'rows'] as const).map((shape) => (
          <Button
            key={shape} size="sm" variant="outline" className="h-6 px-2 text-[11px]"
            disabled={!canPick} onClick={() => onScope(shape)} title={SHAPE_HELP[shape]}
          >
            {SHAPE_LABEL[shape]}
          </Button>
        ))}
      </div>
      {scope && (
        <div className="flex flex-col gap-0.5 rounded bg-muted/50 px-2 py-1">
          <code className="truncate text-[11px]" title={scope.selector}>
            {scope.selector}
          </code>
          <span className="text-[11px] text-muted-foreground">
            matches {scope.matched || 1} element{scope.matched === 1 ? '' : 's'}
          </span>
          {scope.preview && scope.preview.length < 40 && (
            // The exact Walmart mistake: a short reading means the selection is
            // probably still on the label rather than the block.
            <span className="text-[11px] text-warning">
              That looks like a heading. Press ↑ to include the content under it.
            </span>
          )}
        </div>
      )}
    </div>
  )
}

/** What a pick reads, and through which attribute. */
function Reads({
  attribute,
  preview,
  onAttribute,
}: {
  attribute: string
  preview: string
  onAttribute: (value: string) => void
}) {
  return (
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
      <span className="text-[11px] text-muted-foreground">{attributeHint(attribute)}</span>
      <span className="text-[11px]">
        {preview ? `→ “${preview}”` : '→ reads nothing'}
      </span>
    </div>
  )
}

/** What was answered, and the one control that matters afterwards. */
function Summary({
  state,
  onAttribute,
  onClear,
}: {
  state: AskState
  onAttribute: (value: string) => void
  onClear: () => void
}) {
  const { read, before, after } = splitAtRead(state.route)
  return (
    <div className="flex flex-col gap-2">
      {state.route.length > 0 && (
        <div className="flex flex-col gap-0.5 rounded bg-muted/50 px-2 py-1 text-[11px]">
          <span className="text-muted-foreground">
            {before.length} click{before.length === 1 ? '' : 's'} to reach it
            {read ? ', then read it' : ''}
            {after.length ? `, then ${after.length} to close up` : ''}
          </span>
          {read?.text && <span>&rarr; &ldquo;{read.text}&rdquo;</span>}
        </div>
      )}

      {state.pick && (
        <Reads
          attribute={readAttribute(state.pick.candidates) ?? 'text'}
          preview={state.pick.preview}
          onAttribute={onAttribute}
        />
      )}

      {state.answer?.action === 'describe' && (
        <span className="rounded bg-muted/50 px-2 py-1 text-[11px]">
          &ldquo;{state.answer.hint}&rdquo;
        </span>
      )}

      {state.answer?.action === 'skip' && (
        <span className="text-[11px] text-muted-foreground">This field will be dropped.</span>
      )}

      {state.answer?.action === 'accept' && (
        <span className="text-[11px] text-muted-foreground">
          Keeping the value it already collected.
        </span>
      )}

      <Button
        size="sm" variant="ghost" className="h-6 self-start px-2 text-[11px]" onClick={onClear}
      >
        Change
      </Button>
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
