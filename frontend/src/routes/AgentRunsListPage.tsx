import { useNavigate } from 'react-router-dom'
import { Bot } from 'lucide-react'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { EmptyState } from '@/components/app/EmptyState'
import { Skeleton } from '@/components/ui/skeleton'
import { statusVariant } from '@/components/app/AgentRunView'
import { useAgentRunsList } from '@/hooks/useAgentRuns'
import { useAuth } from '@/lib/auth/AuthContext'

function relativeTime(iso: string | null): string {
  if (!iso) return '--'
  const minutes = Math.round((Date.now() - new Date(iso).getTime()) / 60_000)
  if (minutes < 1) return 'just now'
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.round(minutes / 60)
  if (hours < 24) return `${hours}h ago`
  return `${Math.round(hours / 24)}d ago`
}

export function AgentRunsListPage() {
  const navigate = useNavigate()
  const { isAuthed } = useAuth()
  const { data, isLoading, isError } = useAgentRunsList()
  const runs = data?.runs ?? []

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold">Agent runs</h1>
          <p className="text-sm text-muted-foreground">
            Autonomous browser-agent runs -- open one to replay its reasoning, tools, and result.
          </p>
        </div>
        <Button onClick={() => navigate('/playground/agent')}>
          <Bot className="size-4" />
          New run
        </Button>
      </div>

      {!isAuthed ? (
        <EmptyState
          icon={<Bot className="size-8" />}
          title="Tenant API key required"
          description="Sign in with a tenant API key to view agent runs -- an admin token alone doesn't carry tenant scope."
        />
      ) : isLoading ? (
        <div className="flex flex-col gap-2">
          {[0, 1, 2].map((i) => (
            <Skeleton key={i} className="h-12" />
          ))}
        </div>
      ) : isError ? (
        <EmptyState title="Couldn't load agent runs" description="Check that the gateway is reachable and your api key is valid." />
      ) : runs.length === 0 ? (
        <EmptyState
          icon={<Bot className="size-8" />}
          title="No agent runs yet"
          description="Start one from the playground to give an agent a task and watch it work."
          action={
            <Button onClick={() => navigate('/playground/agent')}>
              <Bot className="size-4" />
              New run
            </Button>
          }
        />
      ) : (
        <div className="rounded-lg border border-border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Task</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Steps</TableHead>
                <TableHead>Started</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {runs.map((r) => (
                <TableRow
                  key={r.run_id}
                  className="cursor-pointer"
                  onClick={() => navigate(`/agent-runs/${r.run_id}`)}
                >
                  <TableCell className="max-w-md truncate font-medium">{r.task}</TableCell>
                  <TableCell>
                    <Badge variant={statusVariant(r.status)} className="capitalize">
                      {r.status}
                    </Badge>
                  </TableCell>
                  <TableCell className="tabular-nums text-muted-foreground">
                    {r.current_step} / {r.max_steps}
                  </TableCell>
                  <TableCell className="text-muted-foreground">{relativeTime(r.started_at ?? r.created_at)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}
    </div>
  )
}
