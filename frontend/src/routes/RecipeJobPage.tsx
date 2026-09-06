import { useEffect, useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { ArrowLeft, Download, Loader2 } from 'lucide-react'
import { useRecipeJob } from '@/hooks/useMarketplace'
import { EmptyState } from '@/components/app/EmptyState'
import { JobStatusBadge } from '@/components/app/JobStatusBadge'
import { CopyButton } from '@/components/app/CopyButton'
import { Badge, type BadgeProps } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { useAuth } from '@/lib/auth/AuthContext'
import type { RecipeFieldStatus, RecipeJobResultOut, RunStatus } from '@/lib/api/types'
import { cn } from '@/lib/utils'

/**
 * One submission's results, a row per URL.
 *
 * The organising idea is that a batch is *not* one status. A run can complete
 * and still have lost a field, and the per-field verdict the v2 engine returns
 * is the only thing that says so -- a green "completed" over a row missing its
 * price is exactly the lie this page exists to avoid. So each row carries both
 * its run status and, when the engine had something to say, which fields
 * actually resolved.
 */

function runVariant(status: RunStatus): NonNullable<BadgeProps['variant']> {
  switch (status) {
    case 'completed':
      return 'success'
    case 'failed':
      return 'destructive'
    case 'running':
      return 'accent'
    default:
      return 'outline'
  }
}

/** `fallback` is not a problem; `suspect`/`empty`/`failed` are, to degrees. */
const FIELD_TONE: Record<RecipeFieldStatus, string> = {
  resolved: 'text-muted-foreground',
  fallback: 'text-muted-foreground',
  suspect: 'text-warning',
  empty: 'text-warning',
  failed: 'text-destructive',
}

function ResultRow({ result }: { result: RecipeJobResultOut }) {
  const [open, setOpen] = useState(false)
  const lost = Object.entries(result.field_status ?? {}).filter(
    ([, status]) => status !== 'resolved' && status !== 'fallback',
  )
  const hasData = result.data != null && Object.keys(result.data).length > 0

  return (
    <div className="rounded-md border border-border">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 p-2.5 text-left hover:bg-muted/50"
      >
        <Badge variant={runVariant(result.status)} className="shrink-0 capitalize">
          {result.status}
        </Badge>
        <span className="min-w-0 flex-1 truncate font-mono text-xs">{result.url}</span>
        {lost.length > 0 && (
          <span className="shrink-0 text-[11px] text-warning">
            {lost.length} field{lost.length === 1 ? '' : 's'} missing
          </span>
        )}
      </button>

      {open && (
        <div className="flex flex-col gap-2 border-t border-border p-2.5">
          {result.error && (
            <p className="rounded-md border border-destructive/40 bg-destructive/10 px-2 py-1.5 text-xs">
              {result.error}
            </p>
          )}

          {result.field_status && (
            <div className="flex flex-wrap gap-1">
              {Object.entries(result.field_status).map(([name, status]) => (
                <span
                  key={name}
                  className={cn('font-mono text-[10px]', FIELD_TONE[status])}
                  title={`${name}: ${status}`}
                >
                  {name}:{status}
                </span>
              ))}
            </div>
          )}

          {hasData ? (
            <div className="relative">
              <div className="absolute right-1 top-1">
                <CopyButton text={JSON.stringify(result.data, null, 2)} />
              </div>
              <pre className="max-h-80 overflow-auto whitespace-pre-wrap rounded-md border border-border p-2 text-xs">
                {JSON.stringify(result.data, null, 2)}
              </pre>
            </div>
          ) : (
            result.status === 'completed' && (
              <p className="text-xs text-muted-foreground">
                The run finished and returned nothing. Usually the URL is a different page type
                from the one this recipe reads.
              </p>
            )
          )}
        </div>
      )}
    </div>
  )
}

export function RecipeJobPage() {
  const { recipeId = '', jobId = '' } = useParams<{ recipeId: string; jobId: string }>()
  const { isAuthed } = useAuth()
  const { data, isLoading } = useRecipeJob(recipeId, jobId)

  /**
   * Every completed row's data, as one JSON array.
   *
   * The whole point of a batch is the collection, so the export is the
   * collection -- not one file per URL. `_url` is added rather than assumed:
   * once the rows are pulled out of this page nothing else says which page
   * each came from.
   */
  const exportHref = useMemo(() => {
    const rows = (data?.results ?? [])
      .filter((r) => r.status === 'completed' && r.data)
      .map((r) => ({ _url: r.url, ...r.data }))
    if (rows.length === 0) return null
    return URL.createObjectURL(
      new Blob([JSON.stringify(rows, null, 2)], { type: 'application/json' }),
    )
  }, [data])

  // A blob URL is a live reference the browser holds until it is revoked, and
  // this page re-renders on every poll of a running job -- minting one per
  // render would leak the whole result set, repeatedly, for as long as the job
  // runs. Memoised so it is minted once per result change, and revoked when it
  // is replaced or the page goes away.
  useEffect(() => {
    if (!exportHref) return
    return () => URL.revokeObjectURL(exportHref)
  }, [exportHref])

  if (!isAuthed) {
    return <EmptyState title="Tenant API key required" description="Sign in with a tenant API key to view this job." />
  }
  if (isLoading) return <Skeleton className="h-64" />
  if (!data) {
    return (
      <EmptyState
        title="Job not found"
        action={
          <Button variant="outline" asChild>
            <Link to="/marketplace">Back to the marketplace</Link>
          </Button>
        }
      />
    )
  }

  const { job, results } = data
  const done = job.completed + job.failed
  const rowsWithData = results.filter((r) => r.status === 'completed' && r.data).length

  return (
    <div className="flex flex-col gap-4">
      <div>
        <Button variant="ghost" size="sm" asChild className="-ml-2">
          <Link to={`/marketplace/${recipeId}`}>
            <ArrowLeft className="size-3.5" />
            {job.recipe_name}
          </Link>
        </Button>
      </div>

      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <h1 className="flex items-center gap-2 text-xl font-semibold">
            {job.total} URL{job.total === 1 ? '' : 's'}
            <JobStatusBadge status={job.status} />
            {job.status === 'running' && <Loader2 className="size-4 animate-spin text-muted-foreground" />}
          </h1>
          <p className="text-sm text-muted-foreground">
            {/* The version is recorded per job because a recipe is healed and
                re-versioned underneath the catalogue entry. */}
            Ran with v{job.recipe_version} · {new Date(job.created_at).toLocaleString()}
          </p>
        </div>
        {exportHref && (
          <Button variant="outline" asChild>
            <a
              href={exportHref}
              download={`${job.recipe_name.replace(/[^\w-]+/g, '_')}-${job.job_id.slice(0, 8)}.json`}
            >
              <Download className="size-4" />
              Download {rowsWithData} row{rowsWithData === 1 ? '' : 's'}
            </a>
          </Button>
        )}
      </div>

      <div className="flex flex-wrap gap-4 rounded-lg border border-border p-3 text-sm">
        <span>
          <span className="font-medium">{done}</span>
          <span className="text-muted-foreground">/{job.total} finished</span>
        </span>
        <span className="text-muted-foreground">{job.completed} succeeded</span>
        {job.failed > 0 && <span className="text-destructive">{job.failed} failed</span>}
        {job.queued > 0 && <span className="text-muted-foreground">{job.queued} queued</span>}
        {job.running > 0 && <span className="text-muted-foreground">{job.running} running</span>}
      </div>

      <div className="flex flex-col gap-1.5">
        {results.map((result) => (
          <ResultRow key={result.run_id} result={result} />
        ))}
      </div>
    </div>
  )
}
