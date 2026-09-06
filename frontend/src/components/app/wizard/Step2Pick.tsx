import { List, MousePointer2 } from 'lucide-react'
import { PickerControls } from './PickerControls'
import type { PickerStatus } from '@/hooks/usePagePicker'
import type { PickerMode } from '@/lib/picker/protocol'
import { cn } from '@/lib/utils'

interface Props {
  mode: Exclude<PickerMode, 'single'>
  onModeChange: (mode: Exclude<PickerMode, 'single'>) => void
  status: PickerStatus
  fieldCount: number
  onStart: () => void
  onCancel: () => void
  onRefine: (key: 'ArrowUp' | 'ArrowDown' | 'Enter') => void
}

const MODES: { id: Exclude<PickerMode, 'single'>; label: string; hint: string; icon: typeof List }[] = [
  {
    id: 'list',
    label: 'Repeating list',
    hint: 'Click one card and every matching one is captured — search results, listings, tables.',
    icon: List,
  },
  {
    id: 'detail',
    label: 'Single fields',
    hint: 'Click each value you want, one at a time — a product page, an article.',
    icon: MousePointer2,
  },
]

/**
 * The picking controls. The page they act on is the left pane, always visible.
 *
 * Nothing here renders the page or an overlay: the picker draws its highlight
 * inside the remote page, so the screencast on the left already shows it, and
 * `interact` mode already forwards the author's mouse into it as real input.
 */
export function Step2Pick({
  mode,
  onModeChange,
  status,
  fieldCount,
  onStart,
  onCancel,
  onRefine,
}: Props) {
  const picking = status !== 'idle'

  return (
    <div className="flex flex-col gap-3 p-3">
      <div className="flex flex-col gap-1.5">
        {MODES.map((m) => (
          <button
            key={m.id}
            type="button"
            disabled={picking}
            onClick={() => onModeChange(m.id)}
            className={cn(
              'flex items-start gap-2.5 rounded-md border p-2.5 text-left transition-colors',
              mode === m.id ? 'border-accent bg-accent/10' : 'border-border hover:bg-muted',
              picking && 'opacity-60',
            )}
          >
            <m.icon className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
            <span className="flex min-w-0 flex-col gap-0.5">
              <span className="text-xs font-medium">{m.label}</span>
              <span className="text-[11px] leading-snug text-muted-foreground">{m.hint}</span>
            </span>
          </button>
        ))}
      </div>

      <div className="flex flex-col gap-2 rounded-md border border-dashed border-border p-2.5">
        <PickerControls
          status={status}
          label={mode === 'list' ? 'Pick a list item' : 'Pick a field'}
          onStart={onStart}
          onCancel={onCancel}
          onRefine={onRefine}
        />
        <p className="text-[11px] leading-snug text-muted-foreground">
          {picking
            ? 'Move the pointer over the page on the left — the highlight follows it, one round trip behind. Use Wider / Narrower to adjust, then Use this.'
            : 'Navigate the page on the left to a sample URL first, then start picking.'}
        </p>
      </div>

      {fieldCount > 0 && (
        <p className="text-[11px] text-muted-foreground">
          {fieldCount} field{fieldCount === 1 ? '' : 's'} picked. In single-field mode you can keep
          picking &mdash; each one is added to the list. Name them on the Fields step.
        </p>
      )}
    </div>
  )
}
