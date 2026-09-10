import { ChevronDown, ChevronUp, CornerDownLeft, MousePointerClick, Pin, X } from 'lucide-react'
import { Button } from '@/components/ui/button'
import type { PickerStatus } from '@/hooks/usePagePicker'

interface Props {
  status: PickerStatus
  label: string
  onStart: () => void
  onCancel: () => void
  onRefine: (key: 'ArrowUp' | 'ArrowDown' | 'Enter' | 'Unpin') => void
  disabled?: boolean
}

/**
 * Start/stop a pick, and refine the hovered element without the keyboard.
 *
 * The refine buttons are not a convenience duplicate of the picker's own
 * ↑/↓/Enter shortcuts -- they are the only way to reach them from here. The
 * live view forwards keystrokes to the page only when nothing editable has
 * focus, and during a pick the studio panel generally does, so those keys
 * never leave the browser. The extension hit the same wall from its side
 * panel and added an explicit action message for exactly this.
 *
 * That indirection is also why the picker has to PIN what Wider/Narrower
 * select. Reaching these buttons means dragging the cursor across the live
 * view, and every pixel of that journey is a forwarded `mousemove` -- which
 * used to reset the selection back to whatever was underneath, so Wider
 * appeared to work and Use-this committed the leaf anyway. See
 * `VisualElementPicker.pinned`.
 */
export function PickerControls({ status, label, onStart, onCancel, onRefine, disabled }: Props) {
  if (status === 'idle') {
    return (
      <Button size="sm" onClick={onStart} disabled={disabled}>
        <MousePointerClick className="size-3.5" />
        {label}
      </Button>
    )
  }

  return (
    <div className="flex flex-wrap items-center gap-1.5">
      <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
        <span className="size-2 animate-pulse rounded-full bg-accent" />
        {status === 'installing' ? 'Starting…' : 'Click an element in the page'}
      </span>
      <div className="flex items-center gap-1">
        <Button size="sm" variant="outline" className="h-7 px-2" title="Select the parent element"
          onClick={() => onRefine('ArrowUp')}>
          <ChevronUp className="size-3.5" />
          Wider
        </Button>
        <Button size="sm" variant="outline" className="h-7 px-2" title="Select the first child element"
          onClick={() => onRefine('ArrowDown')}>
          <ChevronDown className="size-3.5" />
          Narrower
        </Button>
        <Button size="sm" variant="outline" className="h-7 px-2" title="Confirm the highlighted element"
          onClick={() => onRefine('Enter')}>
          <CornerDownLeft className="size-3.5" />
          Use this
        </Button>
        <Button
          size="sm"
          variant="ghost"
          className="h-7 px-2"
          title="Wider/Narrower hold the selection still. Click to follow the cursor again."
          onClick={() => onRefine('Unpin')}
        >
          <Pin className="size-3.5" />
          Follow cursor
        </Button>
      </div>
      <Button size="sm" variant="ghost" className="h-7 px-2" onClick={onCancel}>
        <X className="size-3.5" />
        Cancel
      </Button>
    </div>
  )
}
