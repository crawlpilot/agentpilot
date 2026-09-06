import { AlertTriangle, Check, Loader2, Play, X } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { toRows, type PreviewResult, type PreviewStatus } from '@/lib/picker/preview'
import { cn } from '@/lib/utils'

interface Props {
  results: PreviewResult[] | null
  running: boolean
  onRun: () => void
  disabled?: boolean
}

const STATUS_STYLE: Record<PreviewStatus, { label: string; variant: 'success' | 'accent' | 'outline' | 'destructive' | 'warning' }> = {
  resolved: { label: 'resolved', variant: 'success' },
  fallback: { label: 'fallback', variant: 'accent' },
  empty: { label: 'empty', variant: 'outline' },
  failed: { label: 'failed', variant: 'destructive' },
  error: { label: 'bad selector', variant: 'destructive' },
}

/**
 * Run the bindings against the live page and show what actually comes back.
 *
 * This is the validation the whole wizard builds toward. Up to this point
 * every screen shows a *claim* about the page -- this selector matches that
 * card, this column is a price -- and none of them is checked. Here the
 * recipe is executed against the page as it stands and the author sees the
 * data under the names they chose.
 *
 * Two things are shown that a naive preview would hide:
 *
 * - **Which candidate won.** `fallback` means the first choice did not
 *   resolve and something further down the chain did. The value may be
 *   perfectly good, and the recipe is still one step more fragile than it
 *   looks.
 * - **Row alignment.** Columns are read independently (see
 *   `lib/picker/README.md`), so a card missing a price yields a shorter price
 *   column and shifts every value below it. Zipping the columns into rows is
 *   what makes that visible, and it is the single most valuable thing on this
 *   screen.
 */
export function StepPreview({ results, running, onRun, disabled }: Props) {
  const table = results ? toRows(results) : null
  const scalars = results?.filter((r) => !Array.isArray(r.value)) ?? []

  return (
    <div className="flex flex-col gap-3 p-3">
      <div className="flex items-center gap-2">
        <Button size="sm" onClick={onRun} disabled={disabled || running}>
          {running ? <Loader2 className="size-3.5 animate-spin" /> : <Play className="size-3.5" />}
          {running ? 'Running…' : results ? 'Run again' : 'Run on this page'}
        </Button>
        {results && (
          <span className="text-[11px] text-muted-foreground">
            {results.filter((r) => r.status === 'resolved' || r.status === 'fallback').length}/
            {results.length} resolved
          </span>
        )}
      </div>

      {!results && (
        <p className="rounded-md border border-dashed border-border p-3 text-[11px] leading-snug text-muted-foreground">
          Nothing has been checked yet. Everything up to here is a claim about the page &mdash; this
          runs the recipe against it and shows the data under the names you chose.
        </p>
      )}

      {results && table && !table.aligned && (
        <div className="flex items-start gap-2 rounded-md border border-warning/40 bg-warning/10 p-2.5">
          <AlertTriangle className="mt-0.5 size-3.5 shrink-0 text-warning" />
          <div className="min-w-0 text-[11px] leading-snug">
            <p className="font-medium">Columns are not the same length.</p>
            <p className="text-muted-foreground">
              {Object.entries(table.counts)
                .map(([name, n]) => `${name}: ${n}`)
                .join(' · ')}
              . Rows are zipped by position, so a column that skipped a card shifts every value below
              it. Check the short one&rsquo;s selector.
            </p>
          </div>
        </div>
      )}

      {results && scalars.length > 0 && (
        <div className="flex flex-col gap-1">
          <p className="text-[10px] uppercase tracking-wide text-muted-foreground">Single values</p>
          {scalars.map((result) => (
            <div key={result.name} className="flex items-center gap-2 border-b border-border/50 py-1 last:border-0">
              <Badge variant={STATUS_STYLE[result.status].variant} className="shrink-0">
                {STATUS_STYLE[result.status].label}
              </Badge>
              <span className="w-28 shrink-0 truncate font-mono text-[11px]" title={result.name}>
                {result.name}
              </span>
              <span
                className="min-w-0 flex-1 truncate text-[11px] text-muted-foreground"
                title={result.error ?? String(result.value ?? '')}
              >
                {result.error ?? (result.value === null ? '—' : String(result.value))}
              </span>
              {result.candidate !== null && result.candidate > 1 && (
                <span className="shrink-0 text-[10px] text-accent" title="A later candidate resolved this">
                  #{result.candidate}
                </span>
              )}
            </div>
          ))}
        </div>
      )}

      {results && table && table.columns.length > 0 && (
        <div className="flex flex-col gap-1">
          <div className="flex items-center gap-2">
            <p className="text-[10px] uppercase tracking-wide text-muted-foreground">
              Extracted rows
            </p>
            <Badge variant="outline">{table.rows.length}</Badge>
          </div>

          {/* The table scrolls inside its own box: a twelve-column extraction
              must not make the whole panel scroll sideways. */}
          <div className="overflow-x-auto rounded-md border border-border">
            <table className="w-full border-collapse text-[11px]">
              <thead>
                <tr className="border-b border-border bg-muted/50">
                  <th className="px-1.5 py-1 text-right font-normal text-muted-foreground">#</th>
                  {table.columns.map((name) => {
                    const result = results.find((r) => r.name === name)!
                    return (
                      <th key={name} className="whitespace-nowrap px-2 py-1 text-left">
                        <span className="flex items-center gap-1">
                          <span className="font-mono font-medium">{name}</span>
                          {result.status === 'fallback' && (
                            <span className="text-accent" title={`Candidate ${result.candidate} resolved this`}>
                              #{result.candidate}
                            </span>
                          )}
                        </span>
                      </th>
                    )
                  })}
                </tr>
              </thead>
              <tbody>
                {table.rows.slice(0, 25).map((row, i) => (
                  <tr key={i} className="border-b border-border/50 last:border-0">
                    <td className="px-1.5 py-1 text-right text-muted-foreground">{i + 1}</td>
                    {row.map((cell, j) => (
                      <td
                        key={j}
                        className={cn('max-w-56 truncate px-2 py-1', cell === null && 'text-destructive')}
                        title={cell ?? 'missing'}
                      >
                        {cell ?? '—'}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {table.rows.length > 25 && (
            <p className="text-[10px] text-muted-foreground">
              Showing the first 25 of {table.rows.length}.
            </p>
          )}
        </div>
      )}

      {results && (
        <p className="text-[10px] leading-snug text-muted-foreground">
          Values are shown <strong>before transforms</strong>. Transforms (trimming, casting,
          URL resolution, Lua) run server-side at replay, and reproducing them here with different
          code would be a preview that lies.
        </p>
      )}

      {results && results.every((r) => r.status === 'resolved') && (
        <p className="flex items-center gap-1.5 text-[11px] text-success">
          <Check className="size-3.5" />
          Every field resolved on its first candidate.
        </p>
      )}

      {results?.some((r) => r.status === 'error') && (
        <p className="flex items-center gap-1.5 text-[11px] text-destructive">
          <X className="size-3.5" />
          A selector is invalid — the browser could not parse it.
        </p>
      )}
    </div>
  )
}
