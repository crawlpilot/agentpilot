import { useSessionsList } from '@/hooks/useSessionsList'
import { StatCard } from '@/components/app/StatCard'
import { OpenSessionSheet } from '@/components/app/OpenSessionSheet'
import { EmptyState } from '@/components/app/EmptyState'
import { Skeleton } from '@/components/ui/skeleton'
import { useAuth } from '@/lib/auth/AuthContext'

export function DashboardPage() {
  const { isAuthed } = useAuth()
  const { data, isLoading } = useSessionsList()
  const sessions = data?.sessions ?? []
  const active = sessions.filter((s) => s.state === 'active').length
  const idle = sessions.length - active
  const openedLastHour = sessions.filter(
    (s) => s.lease_expires_at !== null && s.lease_expires_at * 1000 > Date.now() - 60 * 60 * 1000,
  ).length

  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold">Dashboard</h1>
          <p className="text-sm text-muted-foreground">Start a session, or check what's running.</p>
        </div>
        <OpenSessionSheet />
      </div>

      {!isAuthed ? (
        <EmptyState
          title="Tenant API key required"
          description="Sign in with a tenant API key to see session stats -- try Nodes or API Keys instead."
        />
      ) : isLoading ? (
        <div className="grid grid-cols-3 gap-4">
          {[0, 1, 2].map((i) => (
            <Skeleton key={i} className="h-24" />
          ))}
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
          <StatCard label="Active sessions" value={active} />
          <StatCard label="Idle (warm)" value={idle} />
          <StatCard label="Opened, last hour" value={openedLastHour} />
        </div>
      )}
    </div>
  )
}
