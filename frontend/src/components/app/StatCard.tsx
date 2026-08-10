import type { ReactNode } from 'react'
import type { LucideIcon } from 'lucide-react'
import { Card } from '@/components/ui/card'
import { cn } from '@/lib/utils'

export function StatCard({
  label,
  value,
  hint,
  icon: Icon,
  accent = false,
}: {
  label: string
  value: string | number | ReactNode
  hint?: string
  icon?: LucideIcon
  // Tints the icon chip with the brand red -- reserved for the single most
  // important metric on a row so the accent still means something.
  accent?: boolean
}) {
  return (
    <Card className="p-4">
      <div className="flex items-center justify-between gap-2">
        <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">{label}</p>
        {Icon && (
          <span
            className={cn(
              'grid size-7 shrink-0 place-items-center rounded-md',
              accent ? 'bg-accent-tint text-accent' : 'bg-muted text-muted-foreground',
            )}
          >
            <Icon className="size-4" />
          </span>
        )}
      </div>
      <p className="mt-3 text-2xl font-semibold tabular-nums">{value}</p>
      {hint && <p className="mt-1 text-xs text-muted-foreground">{hint}</p>}
    </Card>
  )
}
