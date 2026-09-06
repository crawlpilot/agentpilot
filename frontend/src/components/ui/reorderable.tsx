import { useState } from 'react'
import { GripVertical } from 'lucide-react'
import { cn } from '@/lib/utils'

interface ReorderableProps {
  /** Position of this row in its list. */
  index: number
  /** Length of the list, so the last row knows it cannot move down. */
  count: number
  /**
   * Distinguishes concurrent lists on one screen. A candidate dragged out of
   * `price` must not drop into `title`, and both are rendered at once.
   */
  group: string
  onMove: (from: number, to: number) => void
  children: React.ReactNode
  className?: string
  /** Announced on the handle, e.g. "candidate 2 of 4". */
  label: string
}

/**
 * A drag-to-reorder row, keyboard-operable.
 *
 * Candidate priority and step order are both semantic -- the first candidate
 * that resolves wins, and steps run in sequence -- so reordering is an edit,
 * not a view preference. It needs to be as reachable by keyboard as by
 * mouse, which rules out a pointer-only implementation.
 *
 * Deliberately not a DnD library: this is a single-axis list with a handle.
 * The HTML5 drag events plus Alt+Arrow cover it in a few dozen lines, and
 * `docs/recipe-studio.md` asks for the smallest thing that works.
 */
export function Reorderable({ index, count, group, onMove, children, className, label }: ReorderableProps) {
  const [dropSide, setDropSide] = useState<'above' | 'below' | null>(null)

  function payload(): string {
    return JSON.stringify({ group, index })
  }

  function readIndex(e: React.DragEvent): number | null {
    try {
      const data = JSON.parse(e.dataTransfer.getData('text/plain')) as { group: string; index: number }
      return data.group === group ? data.index : null
    } catch {
      return null
    }
  }

  return (
    <div
      className={cn(
        'relative flex items-start gap-2',
        dropSide === 'above' && 'before:absolute before:-top-px before:left-0 before:right-0 before:h-0.5 before:bg-accent',
        dropSide === 'below' && 'after:absolute after:-bottom-px after:left-0 after:right-0 after:h-0.5 after:bg-accent',
        className,
      )}
      onDragOver={(e) => {
        // `readIndex` returns null on dragover in some browsers (the payload
        // is only readable on drop), so fall back to showing the marker and
        // let the drop handler do the real group check.
        e.preventDefault()
        const rect = e.currentTarget.getBoundingClientRect()
        setDropSide(e.clientY < rect.top + rect.height / 2 ? 'above' : 'below')
      }}
      onDragLeave={() => setDropSide(null)}
      onDrop={(e) => {
        e.preventDefault()
        const from = readIndex(e)
        const side = dropSide
        setDropSide(null)
        if (from === null || from === index) return
        // `onMove` splices the row out before inserting it, so a row dragged
        // downward leaves a hole that shifts every later index back by one.
        // These two expressions are that correction, not an off-by-one.
        const to = side === 'above' ? (from < index ? index - 1 : index) : from < index ? index : index + 1
        onMove(from, to)
      }}
    >
      <button
        type="button"
        draggable
        aria-label={`Reorder ${label}. Use Alt with the arrow keys, or drag.`}
        onDragStart={(e) => e.dataTransfer.setData('text/plain', payload())}
        onKeyDown={(e) => {
          if (!e.altKey) return
          if (e.key === 'ArrowUp' && index > 0) {
            e.preventDefault()
            onMove(index, index - 1)
          } else if (e.key === 'ArrowDown' && index < count - 1) {
            e.preventDefault()
            onMove(index, index + 1)
          }
        }}
        className="mt-1.5 cursor-grab rounded p-0.5 text-muted-foreground hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent active:cursor-grabbing"
      >
        <GripVertical className="size-3.5" />
      </button>
      <div className="min-w-0 flex-1">{children}</div>
    </div>
  )
}
