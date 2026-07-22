import { cn } from '@/lib/utils'
import type { SnapshotNode } from '@/lib/api/types'

interface Props {
  node: SnapshotNode
  depth?: number
  onPickRef?: (ref: string) => void
  selectedRef?: string | null
}

export function SnapshotTree({ node, depth = 0, onPickRef, selectedRef }: Props) {
  const clickable = Boolean(onPickRef) && node.ref !== ''
  return (
    <div>
      <div
        role={clickable ? 'button' : undefined}
        onClick={clickable ? () => onPickRef!(node.ref) : undefined}
        style={{ paddingLeft: depth * 14 }}
        className={cn(
          'flex items-baseline gap-2 rounded px-1.5 py-0.5 font-mono text-xs',
          clickable && 'cursor-pointer hover:bg-muted',
          selectedRef && node.ref === selectedRef && 'bg-accent-tint text-accent',
        )}
      >
        {node.ref && <span className="text-muted-foreground">{node.ref}</span>}
        <span className="font-semibold">{node.role}</span>
        {node.name && <span className="truncate text-muted-foreground">"{node.name}"</span>}
      </div>
      {node.children.map((child, i) => (
        <SnapshotTree key={i} node={child} depth={depth + 1} onPickRef={onPickRef} selectedRef={selectedRef} />
      ))}
    </div>
  )
}
