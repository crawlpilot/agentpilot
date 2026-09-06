import { useState } from 'react'
import { ChevronDown, ChevronRight, Trash2 } from 'lucide-react'
import { Input } from '@/components/ui/input'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { LocatorEditor } from '@/components/app/studio/LocatorEditor'
import { PredicateList } from '@/components/app/studio/PredicateEditor'
import { TransformList } from '@/components/app/studio/TransformEditor'
import { describeLocator } from '@/lib/recipe/document'
import { STRUCTURED_KINDS, type Candidate, type PageVariant } from '@/lib/recipe/types'

interface Props {
  candidate: Candidate
  position: number
  onChange: (next: Candidate) => void
  onRemove: () => void
  variants: PageVariant[]
  luaEnabled: boolean
  /** For the per-candidate transform list's drag group id. */
  groupKey: string
}

export function CandidateRow({
  candidate,
  position,
  onChange,
  onRemove,
  variants,
  luaEnabled,
  groupKey,
}: Props) {
  const [expanded, setExpanded] = useState(false)
  const structured = STRUCTURED_KINDS.includes(candidate.locator.kind)
  const verified = candidate.verified_on ?? 0

  return (
    <div className="rounded-md border border-border p-1.5">
      <div className="flex flex-wrap items-center gap-1.5">
        <Button size="icon" variant="ghost" className="size-6" onClick={() => setExpanded(!expanded)}>
          {expanded ? <ChevronDown className="size-3.5" /> : <ChevronRight className="size-3.5" />}
        </Button>
        <span className="w-4 text-center text-[11px] text-muted-foreground">{position}</span>
        <Badge variant={structured ? 'accent' : 'outline'} title={structured ? 'page-embedded JSON -- survives a CSS redesign' : undefined}>
          {candidate.locator.kind}
        </Badge>
        <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-muted-foreground">
          {describeLocator(candidate.locator)}
        </span>
        {candidate.variant_id && <Badge variant="outline">{candidate.variant_id}</Badge>}
        {(candidate.when ?? []).length > 0 && <Badge variant="outline">guarded</Badge>}
        {(candidate.transform ?? null) !== null && <Badge variant="outline">own transform</Badge>}
        <Badge
          variant={verified === 0 ? 'warning' : 'success'}
          title={
            verified === 0
              ? 'Never seen to resolve on a sample page'
              : `Resolved on ${verified} sample page${verified === 1 ? '' : 's'}`
          }
        >
          {verified === 0 ? 'unverified' : `${verified}×`}
        </Badge>
        <Button size="icon" variant="ghost" className="size-6" onClick={onRemove}>
          <Trash2 className="size-3.5" />
        </Button>
      </div>

      {expanded && (
        <div className="mt-2 flex flex-col gap-2.5 border-t border-border/60 pt-2 pl-7">
          <LocatorEditor locator={candidate.locator} onChange={(locator) => onChange({ ...candidate, locator })} />

          <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
            <label className="flex items-center gap-1.5" title="Lower is tried first">
              priority
              <Input
                type="number"
                className="h-7 w-20 text-xs"
                value={candidate.priority ?? 100}
                onChange={(e) => onChange({ ...candidate, priority: Number(e.target.value) })}
              />
            </label>
            <label className="flex items-center gap-1.5">
              only on variant
              <Select
                value={candidate.variant_id ?? '__any__'}
                onValueChange={(v) => onChange({ ...candidate, variant_id: v === '__any__' ? null : v })}
              >
                <SelectTrigger className="h-7 w-40 text-xs">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="__any__">any variant</SelectItem>
                  {variants.map((v) => (
                    <SelectItem key={v.variant_id} value={v.variant_id}>
                      {v.variant_id}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </label>
            <Input
              className="h-7 w-56 text-xs"
              placeholder="note -- why this candidate exists"
              value={candidate.note ?? ''}
              onChange={(e) => onChange({ ...candidate, note: e.target.value || null })}
            />
          </div>

          <div className="flex flex-col gap-1">
            <span className="text-[11px] font-medium text-muted-foreground">Try only when</span>
            <PredicateList
              predicates={candidate.when ?? []}
              onChange={(when) => onChange({ ...candidate, when })}
              label="add guard"
              hint="unguarded"
            />
          </div>

          <div className="flex flex-col gap-1">
            <div className="flex items-center gap-2">
              <span className="text-[11px] font-medium text-muted-foreground">Transform override</span>
              <label className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
                <input
                  type="checkbox"
                  checked={candidate.transform != null}
                  onChange={(e) => onChange({ ...candidate, transform: e.target.checked ? [] : null })}
                />
                use its own pipeline
              </label>
            </div>
            {candidate.transform != null ? (
              <TransformList
                transforms={candidate.transform}
                onChange={(transform) => onChange({ ...candidate, transform })}
                group={`${groupKey}:transform`}
                luaEnabled={luaEnabled}
              />
            ) : (
              <span className="text-[11px] text-muted-foreground">
                inherits the field's pipeline. Override when this source needs different cleanup -- JSON-LD gives
                "1299.00" where the DOM gives "₹ 1,299.00", and one pipeline cannot handle both.
              </span>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
