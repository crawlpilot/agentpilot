import { Badge, type BadgeProps } from '@/components/ui/badge'
import type { RecipeJobStatus } from '@/lib/api/types'

/**
 * `partial` gets its own colour, and that is the whole reason this exists.
 *
 * Nine of ten URLs yielding is a useful result, and showing it in the same red
 * as a job where nothing worked tells the reader to throw away nine pages of
 * data. Amber says what is true: there is something here, and something is
 * missing.
 */
const VARIANTS: Record<RecipeJobStatus, NonNullable<BadgeProps['variant']>> = {
  running: 'accent',
  completed: 'success',
  partial: 'warning',
  failed: 'destructive',
}

export function JobStatusBadge({ status }: { status: RecipeJobStatus }) {
  return (
    <Badge variant={VARIANTS[status]} className="capitalize">
      {status}
    </Badge>
  )
}
