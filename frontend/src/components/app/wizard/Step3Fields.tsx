import { useState } from 'react'
import { ChevronDown, ChevronRight, Sparkles, Trash2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Badge } from '@/components/ui/badge'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { EmptyState } from '@/components/app/EmptyState'
import { CandidateChain } from './CandidateChain'
import { describeTypeSpec } from '@/lib/recipe/document'
import type { FieldDraft } from '@/lib/recipe/fromPick'
import type { Candidate, ValueType } from '@/lib/recipe/types'

interface Props {
  drafts: FieldDraft[]
  onChange: (drafts: FieldDraft[]) => void
  onTestSelector?: (selector: string) => Promise<number>
  /** Offer a structured-data path in place of a picked CSS one. */
  onFindInJson?: (draft: FieldDraft, index: number) => void
  jsonProbeReady: boolean
}

const VALUE_TYPES: ValueType[] = [
  'string', 'text', 'number', 'integer', 'float', 'price', 'boolean', 'url', 'date', 'datetime', 'json',
]

/**
 * Name what was picked, check its type, and reorder its fallbacks.
 *
 * The naming is deliberately post-hoc. The picker assigns a name from the DOM
 * (`SchemaGenerator` infers "Title" from a heading, "Price" from a currency
 * pattern) so an author is correcting a guess rather than facing an empty box
 * for every field -- the same choice the extension makes, and the reason a
 * twelve-column table is a minute's work rather than twelve dialogs.
 */
export function Step3Fields({ drafts, onChange, onTestSelector, onFindInJson, jsonProbeReady }: Props) {
  const [expanded, setExpanded] = useState<number | null>(null)

  function patch(index: number, next: Partial<FieldDraft>) {
    onChange(drafts.map((d, i) => (i === index ? { ...d, ...next } : d)))
  }

  function setCandidates(index: number, candidates: Candidate[]) {
    patch(index, { candidates })
  }

  function setValueType(index: number, value_type: ValueType) {
    const draft = drafts[index]
    const type =
      draft.spec.type.kind === 'list'
        ? { ...draft.spec.type, items: { kind: 'scalar' as const, value_type } }
        : { ...draft.spec.type, value_type }
    patch(index, { spec: { ...draft.spec, type } })
  }

  if (drafts.length === 0) {
    return (
      <div className="flex h-full items-center justify-center p-6">
        <EmptyState
          title="Nothing picked yet"
          description="Go back a step and click something on the page. What you pick lands here to be named and checked."
        />
      </div>
    )
  }

  return (
    <div className="mx-auto flex w-full max-w-3xl flex-col gap-2 p-4">
      {drafts.map((draft, index) => {
        const open = expanded === index
        const inner = draft.spec.type.kind === 'list' ? draft.spec.type.items : draft.spec.type
        const structured = draft.candidates.some((c) =>
          ['json_ld', 'hydration', 'meta'].includes(c.locator.kind),
        )
        return (
          <div key={index} className="rounded-md border border-border">
            <div className="flex items-center gap-2 p-2">
              <button
                type="button"
                className="shrink-0 text-muted-foreground"
                onClick={() => setExpanded(open ? null : index)}
                aria-label={open ? 'Collapse' : 'Expand'}
              >
                {open ? <ChevronDown className="size-4" /> : <ChevronRight className="size-4" />}
              </button>

              <Input
                value={draft.name}
                onChange={(e) => patch(index, { name: e.target.value })}
                className="h-7 w-44 font-mono text-xs"
                placeholder="field_name"
              />

              <Select value={inner?.value_type ?? 'string'} onValueChange={(v) => setValueType(index, v as ValueType)}>
                <SelectTrigger className="h-7 w-28 text-xs">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {VALUE_TYPES.map((t) => (
                    <SelectItem key={t} value={t}>{t}</SelectItem>
                  ))}
                </SelectContent>
              </Select>

              <Badge variant="outline" className="shrink-0">
                {describeTypeSpec(draft.spec.type)}
              </Badge>

              {draft.preview && (
                <span className="min-w-0 flex-1 truncate text-[11px] text-muted-foreground" title={draft.preview}>
                  {draft.preview}
                </span>
              )}

              {!structured && jsonProbeReady && onFindInJson && (
                <Button
                  size="sm"
                  variant="ghost"
                  className="h-6 shrink-0 px-1.5 text-[11px]"
                  title="Look for this value in the page's JSON-LD, hydration state or meta tags"
                  onClick={() => onFindInJson(draft, index)}
                >
                  <Sparkles className="size-3" />
                  Find in JSON
                </Button>
              )}

              <Button
                size="sm"
                variant="ghost"
                className="h-6 shrink-0 px-1.5"
                onClick={() => onChange(drafts.filter((_, i) => i !== index))}
                aria-label={`Remove ${draft.name}`}
              >
                <Trash2 className="size-3" />
              </Button>
            </div>

            {open && (
              <div className="border-t border-border px-2 pb-2 pt-1">
                <p className="pb-1 text-[10px] uppercase tracking-wide text-muted-foreground">
                  Fallback chain &mdash; tried in order until one resolves
                </p>
                <CandidateChain
                  group={`wizard:${index}`}
                  candidates={draft.candidates}
                  onChange={(candidates) => setCandidates(index, candidates)}
                  onTest={onTestSelector}
                />
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}
