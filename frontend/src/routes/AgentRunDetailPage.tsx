import { useNavigate, useParams } from 'react-router-dom'
import { ArrowLeft } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { EmptyState } from '@/components/app/EmptyState'
import { AgentRunView } from '@/components/app/AgentRunView'
import { useAgentRunStatus } from '@/hooks/useAgentRuns'
import { useAuth } from '@/lib/auth/AuthContext'

export function AgentRunDetailPage() {
  const { runId } = useParams<{ runId: string }>()
  const navigate = useNavigate()
  const { isAuthed } = useAuth()
  const { data } = useAgentRunStatus(runId ?? null)
  const run = data?.data

  if (!isAuthed) {
    return (
      <EmptyState
        title="Tenant API key required"
        description="Sign in with a tenant API key to view an agent run -- an admin token alone doesn't carry tenant scope."
      />
    )
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-start gap-3">
        <Button variant="outline" size="icon" onClick={() => navigate('/agent-runs')} title="Back to agent runs">
          <ArrowLeft className="size-4" />
        </Button>
        <div className="min-w-0">
          <h1 className="truncate text-xl font-semibold">{run?.task ?? 'Agent run'}</h1>
          <p className="text-sm text-muted-foreground">
            {run ? (
              <>
                <span className="capitalize">{run.status}</span> · run{' '}
                <span className="font-mono">{run.run_id}</span>
              </>
            ) : (
              'Loading…'
            )}
          </p>
        </div>
      </div>

      {runId && <AgentRunView runId={runId} />}
    </div>
  )
}
