import { useEffect, useState, type FormEvent } from 'react'
import { useSearchParams } from 'react-router-dom'
import { Bot } from 'lucide-react'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Button } from '@/components/ui/button'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Textarea } from '@/components/ui/textarea'
import { EmptyState } from '@/components/app/EmptyState'
import { JsonTextareaField } from '@/components/app/JsonTextareaField'
import { RecentRunsList } from '@/components/app/RecentRunsList'
import { AgentRunView } from '@/components/app/AgentRunView'
import { useCreateAgentRun, useAgentRunStatus } from '@/hooks/useAgentRuns'
import { useRecentRuns } from '@/hooks/useRecentRuns'
import { useAuth } from '@/lib/auth/AuthContext'
import { useToast } from '@/components/ui/toast'
import type { Tier } from '@/lib/api/types'

export function PlaygroundAgentTab() {
  const [searchParams, setSearchParams] = useSearchParams()
  const [domain, setDomain] = useState('')
  const [task, setTask] = useState('')
  const [tier, setTier] = useState<Tier>('auto')
  const [maxSteps, setMaxSteps] = useState(50)
  const [outputSchema, setOutputSchema] = useState<Record<string, unknown> | null>(null)
  const [runId, setRunId] = useState<string | null>(searchParams.get('id'))

  const { apiKey } = useAuth()
  const { toast } = useToast()
  const { runs, append, updateByJobId } = useRecentRuns('agent')
  const createRun = useCreateAgentRun()
  // Deduped with AgentRunView's own poll (same query key) -- read here only to
  // keep the local "recent runs" list's status chip in sync.
  const { data: status } = useAgentRunStatus(runId)

  useEffect(() => {
    if (status && runId) updateByJobId(runId, { status: status.data.status })
  }, [status, runId, updateByJobId])

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
          append({ endpoint: 'agent', url: trimmedDomain, status: 'queued', jobId: resp.run_id })
        },
        onError: (err) => toast({ title: 'Agent run failed to start', description: err.message, variant: 'destructive' }),
      },
    )
  }

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

      {runId && <AgentRunView runId={runId} />}

      <RecentRunsList runs={runs} />
    </div>
  )
}
