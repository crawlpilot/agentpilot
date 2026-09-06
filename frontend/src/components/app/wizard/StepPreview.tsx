import { useState } from 'react'
import { AlertTriangle, Check, Loader2, Play, X } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { CopyButton } from '@/components/app/CopyButton'
import {
  toOutputJson,
  type PreviewResult,
  type PreviewRowsResult,
  type PreviewStatus,
} from '@/lib/picker/preview'
import { cn } from '@/lib/utils'

interface Props {
  results: PreviewResult[] | null
  rowResults: PreviewRowsResult[]
  running: boolean
  onRun: () => void
  disabled?: boolean
  revealCount: number
  applyReveal: boolean
  onApplyRevealChange: (apply: boolean) => void
}

const STATUS_STYLE: Record<
  PreviewStatus,
  { label: string; variant: 'success' | 'accent' | 'outline' | 'destructive' }
> = {
  resolved: { label: 'resolved', variant: 'success' },
  fallback: { label: 'fallback', variant: 'accent' },
  empty: { label: 'empty', variant: 'outline' },
  failed: { label: 'failed', variant: 'destructive' },
  error: { label: 'bad selector', variant: 'destructive' },
}

/**
 * Run the recipe against the live page and show what actually comes back.
 *
 * Two views, because they answer different questions and a recipe can pass one
 * while failing the other:
 *
 * - **Rows / values** answers "did each selector resolve?", and shows *which
 *   candidate* did. `fallback` means the first choice missed and something
 *   further down the chain caught it: the value may be perfectly good and the
 *   recipe is one step more fragile than it looks.
 * - **JSON** answers "is this the data I asked for?". A status table can be
 *   entirely green while the shape is wrong -- a column called `span_2`, a
 *   price with the currency still attached, a table where a list was wanted.
 *   Only the payload shows that.
 */
export function StepPreview({
  results,
  rowResults,
  running,
  onRun,
  disabled,
  revealCount,
  applyReveal,
  onApplyRevealChange,
}: Props) {
  const [view, setView] = useState<'table' | 'json'>('table')
  const scalars = results ?? []
  const ran = results !== null
  const json = ran ? toOutputJson(scalars, rowResults) : null

  return (
    <div className="flex flex-col gap-3 p-3">
      <div className="flex flex-wrap items-center gap-2">
        <Button size="sm" onClick={onRun} disabled={disabled || running}>
          {running ? <Loader2 className="size-3.5 animate-spin" /> : <Play className="size-3.5" />}
          {running ? 'Running…' : ran ? 'Run again' : 'Run on this page'}
        </Button>
        {ran && (
          <div className="flex items-center gap-1">
            {(['table', 'json'] as const).map((v) => (
              <Button
                key={v}
                size="sm"
                variant={view === v ? 'default' : 'outline'}
                className="h-6 px-2 text-[11px]"
                onClick={() => setView(v)}
              >
                {v === 'table' ? 'Data' : 'JSON output'}
              </Button>
            ))}
          </div>
        )}
      </div>

      {revealCount > 0 && (
        <label className="flex items-start gap-2 rounded-md border border-border p-2.5 text-[11px] leading-snug">
          <input
            type="checkbox"
            className="mt-0.5 shrink-0"
            checked={applyReveal}
            onChange={(e) => onApplyRevealChange(e.target.checked)}
          />
          <span className="min-w-0">
            <span className="font-medium">
              Run the {revealCount} reveal step{revealCount === 1 ? '' : 's'} first
            </span>
            <span className="block text-muted-foreground">
              A rehearsal, not replay: this clicks from page script, while a real run dispatches a
              trusted browser click. A step that fails here is a genuine problem; one that passes is
              not yet proof.
            </span>
          </span>
        </label>
      )}

      {!ran && (
        <p className="rounded-md border border-dashed border-border p-3 text-[11px] leading-snug text-muted-foreground">
          Nothing has been checked yet. Everything up to here is a claim about the page &mdash; this
          runs the recipe against it and shows the data under the names you chose.
        </p>
      )}

      {ran && view === 'json' && (
        <div className="flex flex-col gap-1">
          <div className="flex items-center gap-2">
            <p className="text-[10px] uppercase tracking-wide text-muted-foreground">
              What a caller receives
            </p>
            <span className="ml-auto"><CopyButton text={JSON.stringify(json, null, 2)} /></span>
          </div>
          <pre className="max-h-[28rem] overflow-auto rounded-md border border-border bg-muted/40 p-2 font-mono text-[11px] leading-relaxed">
            {JSON.stringify(json, null, 2)}
          </pre>
          <p className="text-[10px] leading-snug text-muted-foreground">
            A field that did not resolve is <strong>absent</strong>, exactly as replay leaves it out
            of <code>result.data</code>. Values are pre-transform &mdash; trimming, casting and URL
            resolution run server-side.
          </p>
        </div>
      )}

      {ran && view === 'table' && (
        <>
          {rowResults.map((table) => (
            <div key={table.name} className="flex flex-col gap-1">
              <div className="flex items-center gap-2">
                <p className="font-mono text-[11px] font-medium">{table.name}</p>
                <Badge variant="outline">{table.rows.length} rows</Badge>
                {table.truncated && (
                  <Badge variant="warning" title="The page had more rows than max_iterations">
                    truncated
                  </Badge>
                )}
              </div>

              {table.error && (
                <p className="flex items-center gap-1.5 text-[11px] text-destructive">
                  <X className="size-3.5" />
                  {table.error}
                </p>
              )}

              {table.rows.length > 0 && (
                <div className="overflow-x-auto rounded-md border border-border">
                  <table className="w-full border-collapse text-[11px]">
                    <thead>
                      <tr className="border-b border-border bg-muted/50">
                        <th className="px-1.5 py-1 text-right font-normal text-muted-foreground">#</th>
                        {Object.keys(table.rows[0]).map((col) => (
                          <th key={col} className="whitespace-nowrap px-2 py-1 text-left">
                            <span className="flex items-center gap-1">
                              <span className="font-mono font-medium">{col}</span>
                              {(table.candidates[col] ?? 1) > 1 && (
                                <span
                                  className="text-accent"
                                  title={`Candidate ${table.candidates[col]} resolved this`}
                                >
                                  #{table.candidates[col]}
                                </span>
                              )}
                            </span>
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {table.rows.slice(0, 25).map((row, i) => (
                        <tr key={i} className="border-b border-border/50 last:border-0">
                          <td className="px-1.5 py-1 text-right text-muted-foreground">{i + 1}</td>
                          {Object.keys(table.rows[0]).map((col) => {
                            const cell = row[col]
                            const text = Array.isArray(cell) ? cell.join(', ') : cell
                            return (
                              <td
                                key={col}
                                className={cn('max-w-56 truncate px-2 py-1', text == null && 'text-destructive')}
                                title={text ?? 'missing in this row'}
                              >
                                {text ?? '—'}
                              </td>
                            )
                          })}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}

              {table.rows.length > 25 && (
                <p className="text-[10px] text-muted-foreground">
                  Showing the first 25 of {table.rows.length}.
                </p>
              )}
            </div>
          ))}

          {scalars.length > 0 && (
            <div className="flex flex-col gap-1">
              <p className="text-[10px] uppercase tracking-wide text-muted-foreground">
                Single values
              </p>
              {scalars.map((result) => (
                <div
                  key={result.name}
                  className="flex items-center gap-2 border-b border-border/50 py-1 last:border-0"
                >
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
                    <span
                      className="shrink-0 text-[10px] text-accent"
                      title="A later candidate resolved this"
                    >
                      #{result.candidate}
                    </span>
                  )}
                </div>
              ))}
            </div>
          )}

          {rowResults.some((t) => t.truncated) && (
            <p className="flex items-start gap-1.5 text-[11px] text-warning">
              <AlertTriangle className="mt-0.5 size-3.5 shrink-0" />
              The page has more rows than this recipe will read. Raise max rows, or accept it and
              know the run is partial.
            </p>
          )}

          {scalars.every((r) => r.status === 'resolved') &&
            rowResults.every((t) => t.rows.length > 0 && !t.truncated) && (
              <p className="flex items-center gap-1.5 text-[11px] text-success">
                <Check className="size-3.5" />
                Everything resolved on its first candidate.
              </p>
            )}
        </>
      )}
    </div>
  )
}
