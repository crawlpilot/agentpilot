import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Globe } from 'lucide-react'
import { Badge, type BadgeProps } from '@/components/ui/badge'
import { EmptyState } from '@/components/app/EmptyState'
import { cn } from '@/lib/utils'
import type { RecentRunEntry } from '@/lib/recentRuns'

// Duplicated from ApiKeyTable.tsx's own `relativeTime` -- small enough
// (4 lines) that hoisting it into lib/utils.ts isn't worth the indirection
// for two call sites; revisit if a third shows up.
function relativeTime(iso: string): string {
  const diffMs = Date.now() - new Date(iso).getTime()
  const minutes = Math.round(diffMs / 60_000)
  if (minutes < 1) return 'just now'
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.round(minutes / 60)
  if (hours < 24) return `${hours}h ago`
  return `${Math.round(hours / 24)}d ago`
}

function statusVariant(status: RecentRunEntry['status']): NonNullable<BadgeProps['variant']> {
  switch (status) {
    case 'success':
    case 'completed':
      return 'success'
    case 'error':
    case 'failed':
      return 'destructive'
    case 'scraping':
      return 'accent'
    case 'cancelled':
      return 'outline'
    default:
      return 'default' // 'queued'
  }
}

function Favicon({ url }: { url: string }) {
  const [errored, setErrored] = useState(false)
  let hostname: string | null = null
  try {
    hostname = new URL(url).hostname
  } catch {
    hostname = null
  }
  if (errored || !hostname) return <Globe className="size-4 shrink-0 text-muted-foreground" />
  return (
    // Google's public favicon service -- leaks the target hostname to Google
    // on every render. Acceptable for an internal-tool playground; don't
    // route real customer URLs through this without knowing that trade-off.
    <img
      src={`https://www.google.com/s2/favicons?domain=${hostname}&sz=32`}
      alt=""
      className="size-4 shrink-0"
      onError={() => setErrored(true)}
    />
  )
}

function RunCard({ entry }: { entry: RecentRunEntry }) {
  const navigate = useNavigate()
  const clickable = entry.endpoint === 'crawl' && Boolean(entry.jobId)

  return (
    <div
      className={cn(
        'flex items-center gap-3 rounded-md border border-border p-3 text-sm',
        clickable && 'cursor-pointer hover:bg-muted/50',
      )}
      onClick={clickable ? () => navigate(`/playground/crawl?id=${entry.jobId}`) : undefined}
    >
      <Favicon url={entry.url} />
      <span className="flex-1 truncate font-mono text-xs">{entry.url}</span>
      {entry.formats?.map((f) => (
        <Badge key={f} className="capitalize">
          {f}
        </Badge>
      ))}
      <Badge variant={statusVariant(entry.status)} className="capitalize">
        {entry.status}
      </Badge>
      <span className="whitespace-nowrap text-xs text-muted-foreground">{relativeTime(entry.startedAt)}</span>
    </div>
  )
}

export function RecentRunsList({ runs }: { runs: RecentRunEntry[] }) {
  return (
    <div className="flex flex-col gap-2">
      <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Recent Runs</p>
      {runs.length === 0 ? (
        <EmptyState title="No runs yet" description="Runs you start from this tab will show up here." />
      ) : (
        <div className="flex flex-col gap-2">
          {runs.map((r) => (
            <RunCard key={r.id} entry={r} />
          ))}
        </div>
      )}
    </div>
  )
}
