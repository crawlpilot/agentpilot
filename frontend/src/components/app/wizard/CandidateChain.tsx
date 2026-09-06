import { useState } from 'react'
import { Check, Play, X } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Reorderable } from '@/components/ui/reorderable'
import { SourceBadge } from './SourceBadge'
import { describeLocator, reorderCandidates } from '@/lib/recipe/document'
import type { Candidate } from '@/lib/recipe/types'

interface Props {
  candidates: Candidate[]
  onChange: (candidates: Candidate[]) => void
  onTest?: (selector: string) => Promise<number>
  /** Distinguishes concurrent chains so a drag cannot cross between fields. */
  group: string
}

/**
 * A field's ordered fallback chain.
 *
 * Order is meaningful, not cosmetic: replay walks candidates by priority until
 * one yields, so dragging a candidate up genuinely changes which selector a run
 * uses first. `reorderCandidates` rewrites `priority` to match the new order
 * rather than relying on array position, because position only breaks ties.
 */
export function CandidateChain({ candidates, onChange, onTest, group }: Props) {
  const [results, setResults] = useState<Record<number, number>>({})
  const [testing, setTesting] = useState<number | null>(null)

  async function test(index: number, selector: string) {
    if (!onTest) return
    setTesting(index)
    try {
      const count = await onTest(selector)
      setResults((prev) => ({ ...prev, [index]: count }))
    } finally {
      setTesting(null)
    }
  }

  if (candidates.length === 0) {
    return (
      <p className="px-1 py-2 text-[11px] text-muted-foreground">
        No candidates. This field can never resolve.
      </p>
    )
  }

  return (
    <div>
      {candidates.map((candidate, index) => {
        const selector = candidate.locator.selector
        const count = results[index]
        return (
          <Reorderable
            key={index}
            index={index}
            count={candidates.length}
            group={group}
            label={`candidate ${index + 1} of ${candidates.length}`}
            onMove={(from, to) => onChange(reorderCandidates(candidates, from, to))}
          >
            <div className="flex min-w-0 items-center gap-2 py-1">
              <SourceBadge kind={candidate.locator.kind} />
              <span
                className="min-w-0 flex-1 truncate font-mono text-[11px]"
                title={describeLocator(candidate.locator)}
              >
                {describeLocator(candidate.locator)}
              </span>
              {candidate.note && (
                <span className="hidden shrink-0 text-[10px] text-muted-foreground sm:inline">
                  {candidate.note}
                </span>
              )}
              {count !== undefined && (
                <span
                  className={
                    count > 0
                      ? 'flex shrink-0 items-center gap-0.5 text-[10px] text-success'
                      : 'flex shrink-0 items-center gap-0.5 text-[10px] text-destructive'
                  }
                  title={`${count} match${count === 1 ? '' : 'es'} on the page right now`}
                >
                  {count > 0 ? <Check className="size-3" /> : <X className="size-3" />}
                  {count}
                </span>
              )}
              {onTest && selector && (
                <Button
                  size="sm"
                  variant="ghost"
                  className="h-6 shrink-0 px-1.5"
                  title="Flash this selector's matches in the page"
                  disabled={testing === index}
                  onClick={() => void test(index, selector)}
                >
                  <Play className="size-3" />
                </Button>
              )}
            </div>
          </Reorderable>
        )
      })}
    </div>
  )
}
