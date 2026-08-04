import { useEffect, useState, type FormEvent } from 'react'
import { useSearchParams } from 'react-router-dom'
import { Bot } from 'lucide-react'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Button } from '@/components/ui/button'
import { Badge, type BadgeProps } from '@/components/ui/badge'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Textarea } from '@/components/ui/textarea'
import { EmptyState } from '@/components/app/EmptyState'
import { JsonTextareaField } from '@/components/app/JsonTextareaField'
import { RecentRunsList } from '@/components/app/RecentRunsList'
import { useCreateAgentRun, useAgentRunStatus, useCancelAgentRun } from '@/hooks/useAgentRuns'
import { useRecentRuns } from '@/hooks/useRecentRuns'
import { useAuth } from '@/lib/auth/AuthContext'
import { useToast } from '@/components/ui/toast'
import { getAgentRunStatus } from '@/lib/api/agentRuns'
import type { AgentStepOut, RunStatus, Tier } from '@/lib/api/types'

function statusVariant(status: RunStatus): NonNullable<BadgeProps['variant']> {
  switch (status) {
    case 'completed':
      return 'success'
    case 'failed':
      return 'destructive'
    case 'running':
      return 'accent'
    case 'cancelled':
      return 'outline'
    default:
      return 'default' // 'queued'
  }
}

export function PlaygroundAgentTab() {
  const [searchParams, setSearchParams] = useSearchParams()
  const [domain, setDomain] = useState('')
  const [task, setTask] = useState('')
  const [tier, setTier] = useState<Tier>('auto')
  const [maxSteps, setMaxSteps] = useState(50)
  const [outputSchema, setOutputSchema] = useState<Record<string, unknown> | null>(null)

  const [runId, setRunId] = useState<string | null>(searchParams.get('id'))
  const [steps, setSteps] = useState<Record<number, AgentStepOut>>({})
  const [nextCursor, setNextCursor] = useState<string | null>(null)
  const [paginated, setPaginated] = useState(false)
  const [loadingMore, setLoadingMore] = useState(false)

  const { apiKey } = useAuth()
  const { toast } = useToast()
  const { runs, append, updateByJobId } = useRecentRuns('agent')
  const createRun = useCreateAgentRun()
  const cancelRun = useCancelAgentRun()
  const { data: status } = useAgentRunStatus(runId)

  useEffect(() => {
    if (!status) return
    setSteps((prev) => {
      const next = { ...prev }
      for (const step of status.steps) next[step.seq] = step
      return next
    })
    if (!paginated) setNextCursor(status.next)
    if (runId) updateByJobId(runId, { status: status.data.status })
  }, [status, paginated, runId, updateByJobId])

  if (!apiKey) {
    return (
      <EmptyState
        title="Tenant API key required"
        description="Sign in with a tenant API key to run an agent -- an admin token alone doesn't carry tenant scope."
      />
    )
  }

  function handleSubmit(e: FormEvent) {
    e.preventDefault()
    const trimmedDomain = domain.trim()
    const trimmedTask = task.trim()
    if (!trimmedDomain || !trimmedTask) return
    createRun.mutate(
      {
        domain: trimmedDomain,
        task: trimmedTask,
        tier,
        max_steps: maxSteps,
        output_schema: outputSchema,
      },
      {
        onSuccess: (resp) => {
          setRunId(resp.run_id)
          setSearchParams({ id: resp.run_id })
          setSteps({})
          setNextCursor(null)
          setPaginated(false)
          append({ endpoint: 'agent', url: trimmedDomain, status: 'queued', jobId: resp.run_id })
        },
        onError: (err) => toast({ title: 'Agent run failed to start', description: err.message, variant: 'destructive' }),
      },
    )
  }

  function handleCancel() {
    if (!runId) return
    cancelRun.mutate(runId, {
      onSuccess: () => toast({ title: 'Agent run cancelled' }),
      onError: (err) => toast({ title: 'Cancel failed', description: err.message, variant: 'destructive' }),
    })
  }

  async function loadMore() {
    if (!runId || !nextCursor || !apiKey) return
    setLoadingMore(true)
    try {
      const resp = await getAgentRunStatus(apiKey, runId, nextCursor)
      setSteps((prev) => {
        const next = { ...prev }
        for (const step of resp.steps) next[step.seq] = step
        return next
      })
      setPaginated(true)
      setNextCursor(resp.next)
    } catch (err) {
      toast({
        title: 'Failed to load more',
        description: err instanceof Error ? err.message : String(err),
        variant: 'destructive',
      })
    } finally {
      setLoadingMore(false)
    }
  }

  const stepList = Object.values(steps).sort((a, b) => a.seq - b.seq)
  const run = status?.data
  const isRunning = run ? run.status === 'queued' || run.status === 'running' : false
  const pct = run && run.max_steps > 0 ? Math.round((run.current_step / run.max_steps) * 100) : 0

  return (
    <div className="flex flex-col gap-6">
      <form onSubmit={handleSubmit} className="flex flex-col gap-4">
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="agent-task">Task</Label>
          <Textarea
            id="agent-task"
            rows={2}
            placeholder="e.g. go to the pricing page and report the cheapest plan's monthly price"
            value={task}
            onChange={(e) => setTask(e.target.value)}
            autoFocus
          />
        </div>

        <div className="flex flex-wrap items-end gap-4">
          <div className="flex flex-1 flex-col gap-1.5">
            <Label htmlFor="agent-domain">Domain</Label>
            <Input
              id="agent-domain"
              placeholder="example.com"
              value={domain}
              onChange={(e) => setDomain(e.target.value)}
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label>Tier</Label>
            <Select value={tier} onValueChange={(v) => setTier(v as Tier)}>
              <SelectTrigger className="w-32">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="auto">auto</SelectItem>
                <SelectItem value="basic">basic</SelectItem>
                <SelectItem value="stealth">stealth</SelectItem>
                <SelectItem value="enhanced">enhanced</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="agent-max-steps">Max steps</Label>
            <Input
              id="agent-max-steps"
              type="number"
              min={1}
              className="w-28"
              value={maxSteps}
              onChange={(e) => setMaxSteps(Number(e.target.value) || 1)}
            />
          </div>
          <Button type="submit" disabled={createRun.isPending || !domain.trim() || !task.trim()}>
            <Bot className="size-4" />
            {createRun.isPending ? 'Starting…' : 'Run agent'}
          </Button>
        </div>

        <details className="text-sm text-muted-foreground">
          <summary className="cursor-pointer select-none">Advanced options</summary>
          <div className="mt-3">
            <JsonTextareaField
              label="Output schema (optional)"
              value={outputSchema}
              onChange={setOutputSchema}
              placeholder={'{\n  "type": "object",\n  "properties": { "price": { "type": "number" } }\n}'}
            />
          </div>
        </details>
      </form>

      {run && (
        <div className="flex flex-col gap-2 rounded-md border border-border p-3">
          <div className="flex items-center gap-2">
            <Badge variant={statusVariant(run.status)} className="capitalize">
              {run.status}
            </Badge>
            <span className="text-sm text-muted-foreground">
              step {run.current_step} / {run.max_steps}
            </span>
            <div className="flex-1" />
            <Button size="sm" variant="outline" disabled={!isRunning || cancelRun.isPending} onClick={handleCancel}>
              {cancelRun.isPending ? 'Cancelling…' : 'Cancel'}
            </Button>
          </div>
          <div className="h-2 w-full overflow-hidden rounded-full bg-muted">
            <div className="h-full rounded-full bg-accent transition-all" style={{ width: `${pct}%` }} />
          </div>
        </div>
      )}

      {run && (
        <div className="flex flex-col gap-2">
          <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Result</p>
          {run.status === 'failed' ? (
            <div className="flex items-start gap-2 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm">
              <Badge variant="destructive">failed</Badge>
              <span className="text-muted-foreground">{run.error ?? 'the run failed without an error message'}</span>
            </div>
          ) : run.status === 'completed' ? (
            run.result ? (
              <pre className="max-h-96 overflow-auto whitespace-pre-wrap rounded-md border border-border p-3 text-xs">
                {JSON.stringify(run.result, null, 2)}
              </pre>
            ) : (
              <EmptyState title="No result returned" description="The run completed without a structured result." />
            )
          ) : (
            <EmptyState title="No result yet" description="The agent is still working." />
          )}
        </div>
      )}

      <div className="flex flex-col gap-2">
        <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
          {stepList.length > 0 ? `${stepList.length} steps` : 'Steps'}
        </p>
        {stepList.length === 0 ? (
          <EmptyState title="No steps yet" description="The agent's per-step history will appear here." />
        ) : (
          <div className="flex flex-col gap-2">
            {stepList.map((step) => (
              <details key={step.seq} className="rounded-md border border-border p-3">
                <summary className="flex cursor-pointer select-none items-center gap-2 text-sm">
                  <Badge>step {step.step_number}</Badge>
                  <span className="flex-1 truncate text-xs text-muted-foreground">{step.next_goal ?? ''}</span>
                </summary>
                <div className="mt-3 flex flex-col gap-2 text-sm">
                  {step.evaluation_previous_goal && (
                    <div>
                      <span className="text-muted-foreground">Evaluation: </span>
                      {step.evaluation_previous_goal}
                    </div>
                  )}
                  {step.memory && (
                    <div>
                      <span className="text-muted-foreground">Memory: </span>
                      {step.memory}
                    </div>
                  )}
                  <div>
                    <span className="text-muted-foreground">Actions</span>
                    <pre className="mt-1 max-h-64 overflow-auto whitespace-pre-wrap rounded-md border border-border p-2 text-xs">
                      {JSON.stringify(step.actions, null, 2)}
                    </pre>
                  </div>
                  {step.action_results.length > 0 && (
                    <div>
                      <span className="text-muted-foreground">Results</span>
                      <ul className="mt-1 flex flex-col gap-0.5 text-xs">
                        {step.action_results.map((r, i) => (
                          <li key={i} className="font-mono">
                            {r}
                          </li>
                        ))}
                      </ul>
                    </div>
                  )}
                </div>
              </details>
            ))}
            {nextCursor && (
              <Button variant="outline" size="sm" onClick={loadMore} disabled={loadingMore} className="w-fit">
                {loadingMore ? 'Loading…' : 'Load more'}
              </Button>
            )}
          </div>
        )}
      </div>

      <RecentRunsList runs={runs} />
    </div>
  )
}
