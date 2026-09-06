import { Badge } from '@/components/ui/badge'
import { STRUCTURED_KINDS } from '@/lib/recipe/types'
import type { LocatorKind } from '@/lib/recipe/types'

const LABEL: Record<LocatorKind, string> = {
  json_ld: 'JSON-LD',
  hydration: 'hydration',
  meta: 'meta',
  ax_role: 'role',
  text: 'text',
  css: 'CSS',
  xpath: 'XPath',
}

/**
 * Where a candidate reads from.
 *
 * Structured sources are visually distinct, not decoratively but because the
 * distinction is the point: `docs/recipe-studio.md` asks for a source badge on
 * every proposed candidate precisely so an author can see at a glance that the
 * JSON-LD path offered above their CSS pick is the more durable answer. A
 * picker that presented all seven kinds identically would lead every author to
 * the selector they clicked.
 */
export function SourceBadge({ kind }: { kind: LocatorKind }) {
  const structured = STRUCTURED_KINDS.includes(kind)
  return (
    <Badge variant={structured ? 'accent' : 'outline'} className="shrink-0 font-mono text-[10px]">
      {LABEL[kind]}
    </Badge>
  )
}
