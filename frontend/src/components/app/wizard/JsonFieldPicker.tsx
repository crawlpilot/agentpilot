import { useState } from 'react'
import { Braces, Loader2, Plus, Search } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Badge } from '@/components/ui/badge'
import { findPaths, type PathHit } from '@/lib/recipe/probe'

interface Props {
  hits: PathHit[] | null
  loading: boolean
  onProbe: () => void
  onAdd: (hit: PathHit) => void
}

/**
 * Define a field straight from the page's embedded JSON.
 *
 * The visual picker answers "what selector reaches this element?", and the
 * answer is CSS -- the least durable of the seven locator kinds. This asks the
 * inverted question, "where does this value already live?", and on a page
 * carrying JSON-LD or hydration state the answer is usually a path that
 * outlives the next redesign.
 *
 * It used to exist only in the advanced editor, which meant the *better* way
 * to bind a field was the one an author had to go looking for. On Walmart the
 * right answer for six sections is a hydration path; a flow that only offered
 * clicking would lead every author to the worse one.
 *
 * Search by the value you can see on the page, not by path: an author knows
 * the price reads "£29.99", not that it lives at
 * `props.pageProps.initialData.product.offers[0].price`. Matches are sorted
 * shortest-path-first, because the same string often appears at several depths
 * and the shallow one is usually the stable one.
 */
export function JsonFieldPicker({ hits, loading, onProbe, onAdd }: Props) {
  const [needle, setNeedle] = useState('')
  const matches = hits ? findPaths(hits, needle) : []
  const counts = (hits ?? []).reduce<Record<string, number>>((acc, h) => {
    acc[h.kind] = (acc[h.kind] ?? 0) + 1
    return acc
  }, {})

  return (
    <div className="flex flex-col gap-2 border-t border-border pt-2">
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="mr-auto text-[10px] uppercase tracking-wide text-muted-foreground">
          From page JSON
        </span>
        <Button size="sm" variant="ghost" className="h-6 px-1.5 text-[11px]" onClick={onProbe} disabled={loading}>
          {loading ? <Loader2 className="size-3 animate-spin" /> : <Braces className="size-3" />}
          {hits ? 'Re-read' : 'Read'}
        </Button>
      </div>

      {hits && (
        <div className="flex flex-wrap items-center gap-1">
          {(['json_ld', 'hydration', 'meta'] as const).map((k) => (
            <Badge key={k} variant={counts[k] ? 'accent' : 'outline'} className="text-[10px]">
              {counts[k] ?? 0} {k}
            </Badge>
          ))}
          <span className="text-[10px] text-muted-foreground">{hits.length} values</span>
        </div>
      )}

      {hits && hits.length === 0 && (
        <p className="text-[10px] leading-snug text-muted-foreground">
          Nothing embedded. This page has to be read from the DOM &mdash; which is a real answer,
          not a failure, and it means these fields need more care when the site is redesigned.
        </p>
      )}

      {hits && hits.length > 0 && (
        <>
          <div className="flex items-center gap-1.5">
            <Search className="size-3 shrink-0 text-muted-foreground" />
            <Input
              className="h-6 text-[11px]"
              placeholder="paste a value you can see on the page"
              value={needle}
              onChange={(e) => setNeedle(e.target.value)}
            />
          </div>

          {needle.trim().length >= 2 && matches.length === 0 && (
            <p className="text-[10px] text-muted-foreground">
              Not in the page&rsquo;s JSON. Pick it from the page instead.
            </p>
          )}

          <div className="max-h-48 overflow-y-auto">
            {matches.slice(0, 30).map((hit, i) => (
              <div key={i} className="flex items-center gap-1.5 border-b border-border/50 py-1 last:border-0">
                <Badge variant="accent" className="shrink-0 text-[10px]">
                  {hit.kind}
                </Badge>
                <span className="min-w-0 flex-1 truncate font-mono text-[10px]" title={hit.path}>
                  {hit.path}
                </span>
                <span className="w-24 shrink-0 truncate text-[10px] text-muted-foreground" title={hit.value}>
                  {hit.value}
                </span>
                <Button
                  size="sm"
                  variant="ghost"
                  className="h-5 shrink-0 px-1"
                  aria-label={`Add ${hit.path} as a field`}
                  onClick={() => onAdd(hit)}
                >
                  <Plus className="size-3" />
                </Button>
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  )
}
