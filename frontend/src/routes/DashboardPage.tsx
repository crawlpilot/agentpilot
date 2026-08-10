import { useMemo } from 'react'
import { Link } from 'react-router-dom'
import { Activity, ArrowRight, FileScan, Layers, MousePointerClick, PlugZap, Route as RouteIcon, Server, Zap } from 'lucide-react'
import { useSessionsList } from '@/hooks/useSessionsList'
import { useNodesList } from '@/hooks/useNodes'
import { StatCard } from '@/components/app/StatCard'
import { QuickStartCard } from '@/components/app/QuickStartCard'
import { RecentRunsList } from '@/components/app/RecentRunsList'
import { OpenSessionSheet } from '@/components/app/OpenSessionSheet'
import { EmptyState } from '@/components/app/EmptyState'
import { Skeleton } from '@/components/ui/skeleton'
import { useAuth } from '@/lib/auth/AuthContext'
import { loadRecentRuns } from '@/lib/recentRuns'

const QUICK_START = [
  { to: '/playground/scrape', title: 'Scrape a page', description: 'URL in, clean markdown out', icon: FileScan },
  { to: '/playground/crawl', title: 'Crawl a site', description: 'Follow links across a domain', icon: Layers },
  { to: '/playground/interact', title: 'Interact', description: 'Click, type, and automate', icon: MousePointerClick },
  { to: '/playground/map', title: 'Map a website', description: 'Discover every URL fast', icon: RouteIcon },
]

export function DashboardPage() {
  const { isAuthed, isAdmin, tenant } = useAuth()
  const { data, isLoading } = useSessionsList()
  const { data: nodesData } = useNodesList()

  const sessions = data?.sessions ?? []
  const active = sessions.filter((s) => s.state === 'active').length
  const idle = sessions.length - active
  const openedLastHour = sessions.filter(
    (s) => s.lease_expires_at !== null && s.lease_expires_at * 1000 > Date.now() - 60 * 60 * 1000,
  ).length

  // Recent activity is client-side history (scrape/map/crawl don't persist
  // server-side -- see lib/recentRuns.ts), read straight from localStorage
  // for the signed-in tenant. `useMemo` so it isn't re-parsed on every render.
  const recentRuns = useMemo(() => (tenant ? loadRecentRuns(tenant).slice(0, 6) : []), [tenant])

  // Fleet snapshot -- only meaningful (and only fetched) with an admin token.
  const nodes = nodesData?.nodes ?? []
  const liveNodes = nodes.filter((n) => n.live).length
  const usedContexts = nodes.reduce((sum, n) => sum + (n.active ?? 0), 0)
  const totalContexts = nodes.reduce((sum, n) => sum + (n.max_contexts ?? 0), 0)

  return (
    <div className="mx-auto flex max-w-6xl flex-col gap-8">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold">Dashboard</h1>
          <p className="text-sm text-muted-foreground">Start a session, or check what's running.</p>
        </div>
        <OpenSessionSheet />
      </div>

      {!isAuthed ? (
        <EmptyState
          icon={<PlugZap className="size-8" />}
          title="Tenant API key required"
          description="Sign in with a tenant API key to see session stats -- or head to Nodes / API Keys with your admin token."
        />
      ) : isLoading ? (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
          {[0, 1, 2].map((i) => (
            <Skeleton key={i} className="h-24" />
          ))}
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
          <StatCard label="Active sessions" value={active} icon={Activity} accent hint="Currently driving a browser" />
          <StatCard label="Idle (warm)" value={idle} icon={Zap} hint="Pooled, ready to reuse" />
          <StatCard label="Opened, last hour" value={openedLastHour} icon={RouteIcon} hint="New leases in the last 60m" />
        </div>
      )}

      <section className="flex flex-col gap-3">
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-semibold">Quick start</h2>
          <Link
            to="/playground/scrape"
            className="flex items-center gap-1 text-xs font-medium text-accent hover:underline"
          >
            Open Playground
            <ArrowRight className="size-3.5" />
          </Link>
        </div>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
          {QUICK_START.map((q) => (
            <QuickStartCard key={q.to} {...q} />
          ))}
        </div>
      </section>

      {isAdmin && nodes.length > 0 && (
        <section className="flex flex-col gap-3">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-semibold">Fleet capacity</h2>
            <Link to="/nodes" className="flex items-center gap-1 text-xs font-medium text-accent hover:underline">
              View nodes
              <ArrowRight className="size-3.5" />
            </Link>
          </div>
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
            <StatCard label="Live nodes" value={`${liveNodes} / ${nodes.length}`} icon={Server} />
            <StatCard label="Contexts in use" value={usedContexts} icon={Activity} />
            <StatCard
              label="Total capacity"
              value={totalContexts}
              icon={Layers}
              hint={totalContexts > 0 ? `${Math.round((usedContexts / totalContexts) * 100)}% utilized` : undefined}
            />
          </div>
        </section>
      )}

      {isAuthed && (
        <section className="flex flex-col gap-3">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-semibold">Recent activity</h2>
            <Link to="/sessions" className="flex items-center gap-1 text-xs font-medium text-accent hover:underline">
              All sessions
              <ArrowRight className="size-3.5" />
            </Link>
          </div>
          <RecentRunsList runs={recentRuns} showHeader={false} />
        </section>
      )}
    </div>
  )
}
