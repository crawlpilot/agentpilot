import { Globe } from 'lucide-react'
import { useSessionsList } from '@/hooks/useSessionsList'
import { SessionsTable } from '@/components/app/SessionsTable'
import { OpenSessionSheet } from '@/components/app/OpenSessionSheet'
import { EmptyState } from '@/components/app/EmptyState'
import { Skeleton } from '@/components/ui/skeleton'

export function SessionsListPage() {
  const { data, isLoading, isError } = useSessionsList()
  const sessions = data?.sessions ?? []

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold">Sessions</h1>
          <p className="text-sm text-muted-foreground">
            Every warm browser context this tenant currently holds.
          </p>
        </div>
        <OpenSessionSheet />
      </div>

      {isLoading ? (
        <div className="flex flex-col gap-2">
          {[0, 1, 2].map((i) => (
            <Skeleton key={i} className="h-12" />
          ))}
        </div>
      ) : isError ? (
        <EmptyState title="Couldn't load sessions" description="Check that the gateway is reachable and your api key is valid." />
      ) : sessions.length === 0 ? (
        <EmptyState
          icon={<Globe className="size-8" />}
          title="No sessions yet"
          description="Open one to start browsing through the API."
          action={<OpenSessionSheet />}
        />
      ) : (
        <div className="rounded-lg border border-border">
          <SessionsTable sessions={sessions} />
        </div>
      )}
    </div>
  )
}
