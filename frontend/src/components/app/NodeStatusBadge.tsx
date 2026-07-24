import { Badge } from '@/components/ui/badge'
import type { NodeOut } from '@/lib/api/types'

export function NodeStatusBadge({ live }: { live: NodeOut['live'] }) {
  if (live) return <Badge variant="success">live</Badge>
  return <Badge variant="destructive">stale</Badge>
}
