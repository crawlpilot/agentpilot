import { ArrowDown, ChevronRight, MousePointerClick } from 'lucide-react'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Badge } from '@/components/ui/badge'
import { PickerControls } from './PickerControls'
import type { PickerStatus } from '@/hooks/usePagePicker'
import { cn } from '@/lib/utils'

export type PaginationMode = 'none' | 'next' | 'scroll'

export interface PaginationChoice {
  mode: PaginationMode
  /** Ranked selectors for the next / load-more control. */
  selector?: string
  maxPages: number
}

interface Props {
  value: PaginationChoice
  onChange: (value: PaginationChoice) => void
  status: PickerStatus
  onPickButton: () => void
  onCancel: () => void
  onRefine: (key: 'ArrowUp' | 'ArrowDown' | 'Enter') => void
}

const OPTIONS: { id: PaginationMode; title: string; hint: string; icon: typeof ChevronRight }[] = [
  {
    id: 'none',
    title: 'Single page',
    hint: 'Everything worth having is already on the page.',
    icon: ChevronRight,
  },
  {
    id: 'next',
    title: 'Next / Load more button',
    hint: 'A control that reveals the following page. You point at it, and its selector is derived without the page number baked in.',
    icon: MousePointerClick,
  },
  {
    id: 'scroll',
    title: 'Infinite scroll',
    hint: 'Content appears as the page is scrolled.',
    icon: ArrowDown,
  },
]

/**
 * How to reach the rest of the results.
 *
 * Picking the control uses the picker's `single` mode, which runs the
 * pagination-specific generator: it rejects any selector containing a quoted
 * digit, so "go to page 2" cannot become a recipe that only ever works on page
 * two. That rule is the whole reason this is a pick rather than a text field.
 */
export function Step4Pagination({ value, onChange, status, onPickButton, onCancel, onRefine }: Props) {
  return (
    <div className="flex flex-col gap-2 p-3">
      {OPTIONS.map((option) => {
        const selected = value.mode === option.id
        return (
          <button
            key={option.id}
            type="button"
            onClick={() => onChange({ ...value, mode: option.id })}
            className={cn(
              'flex items-start gap-3 rounded-md border p-3 text-left transition-colors',
              selected ? 'border-accent bg-accent/10' : 'border-border hover:bg-muted',
            )}
          >
            <option.icon className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
            <span className="flex min-w-0 flex-col gap-0.5">
              <span className="text-sm font-medium">{option.title}</span>
              <span className="text-[11px] text-muted-foreground">{option.hint}</span>
            </span>
          </button>
        )
      })}

      {value.mode === 'next' && (
        <div className="flex flex-col gap-2 rounded-md border border-dashed border-border p-3">
          <div className="flex flex-wrap items-center gap-2">
            <PickerControls
              status={status}
              label={value.selector ? 'Pick a different button' : 'Point to the button'}
              onStart={onPickButton}
              onCancel={onCancel}
              onRefine={onRefine}
            />
            {value.selector && <Badge variant="success">selected</Badge>}
          </div>
          {value.selector && (
            <code className="block truncate rounded bg-muted px-2 py-1 font-mono text-[11px]" title={value.selector}>
              {value.selector}
            </code>
          )}
        </div>
      )}

      {value.mode !== 'none' && (
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="max-pages">Maximum pages</Label>
          <Input
            id="max-pages"
            type="number"
            min={1}
            className="w-32"
            value={value.maxPages}
            onChange={(e) => onChange({ ...value, maxPages: Math.max(1, Number(e.target.value) || 1) })}
          />
        </div>
      )}
    </div>
  )
}
