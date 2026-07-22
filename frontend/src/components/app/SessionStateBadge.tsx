import { Badge } from '@/components/ui/badge'
import type { SessionOut } from '@/lib/api/types'

export function SessionStateBadge({ state }: { state: SessionOut['state'] }) {
  if (state === 'active') return <Badge variant="success">active</Badge>
  return <Badge variant="warning">expired</Badge>
}
