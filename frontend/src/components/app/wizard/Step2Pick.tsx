import { List, MousePointer2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { LiveViewPanel } from '@/components/app/LiveViewPanel'
import { PickerControls } from './PickerControls'
import type { PickerStatus } from '@/hooks/usePagePicker'
import type { PickerMode } from '@/lib/picker/protocol'
import { cn } from '@/lib/utils'

interface Props {
  sessionId: string
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
 * The picking step: the live page, with the picker driven over it.
 *
 * There is no separate preview surface and no overlay rendered here. The
 * picker draws its highlight inside the remote page, so the existing
 * screencast already shows it, and `interact` mode already forwards the
 * author's mouse into that page as real input. The studio's contribution is
 * the mode switch and the refine buttons.
 */
export function Step2Pick({
  sessionId,
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
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex shrink-0 flex-wrap items-center gap-2 border-b border-border px-3 py-2">
        <div className="flex items-center gap-1">
          {MODES.map((m) => (
            <Button
              key={m.id}
              size="sm"
              variant={mode === m.id ? 'default' : 'outline'}
              className="h-7"
              disabled={picking}
              title={m.hint}
              onClick={() => onModeChange(m.id)}
            >
              <m.icon className="size-3.5" />
              {m.label}
            </Button>
          ))}
        </div>

        <div className="ml-auto flex items-center gap-2">
          {fieldCount > 0 && (
            <Badge variant="success">
              {fieldCount} field{fieldCount === 1 ? '' : 's'}
            </Badge>
          )}
          <PickerControls
            status={status}
            label={mode === 'list' ? 'Pick a list item' : 'Pick a field'}
            onStart={onStart}
            onCancel={onCancel}
            onRefine={onRefine}
          />
        </div>
      </div>

      <p
        className={cn(
          'shrink-0 px-3 py-1.5 text-[11px]',
          picking ? 'bg-accent/10 text-foreground' : 'text-muted-foreground',
        )}
      >
        {picking
          ? 'Move the pointer over the page — the highlight follows it, one round trip behind. Use Wider / Narrower to adjust, then Use this.'
          : MODES.find((m) => m.id === mode)!.hint}
      </p>

      <div className="min-h-0 flex-1">
        <LiveViewPanel sessionId={sessionId} />
      </div>
    </div>
  )
}
