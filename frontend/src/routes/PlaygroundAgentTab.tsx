import { useEffect, useState, type FormEvent } from 'react'
import { useSearchParams } from 'react-router-dom'
import {
  AlertTriangle,
  Bot,
  Brain,
  CheckCircle2,
  ChevronRight,
  CircleCheck,
  CircleX,
  Loader2,
} from 'lucide-react'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Button } from '@/components/ui/button'
import { Badge, type BadgeProps } from '@/components/ui/badge'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Textarea } from '@/components/ui/textarea'
import { EmptyState } from '@/components/app/EmptyState'
import { JsonTextareaField } from '@/components/app/JsonTextareaField'
import { RecentRunsList } from '@/components/app/RecentRunsList'
import { AgentActionCard } from '@/components/app/AgentActionCard'
import { AgentLiveView } from '@/components/app/AgentLiveView'
import { useCreateAgentRun, useAgentRunStatus, useCancelAgentRun } from '@/hooks/useAgentRuns'
import { useRecentRuns } from '@/hooks/useRecentRuns'
import { useAuth } from '@/lib/auth/AuthContext'
import { useToast } from '@/components/ui/toast'
import { getAgentRunStatus } from '@/lib/api/agentRuns'
import { cn } from '@/lib/utils'
import type { AgentStepOut, RunStatus, Tier } from '@/lib/api/types'

type StepStatus = 'ok' | 'warn' | 'bad'

// The persisted step carries no explicit status, so classify from its result
// strings: an outright failure -> bad, a soft "blocked/skipped/retry" -> warn,
// otherwise ok. Matches the phrasing the loop writes in agentpilot/agent/loop.py.
function stepStatus(step: AgentStepOut): StepStatus {
  const joined = step.action_results.join(' ').toLowerCase()
  if (/\bfailed\b|\berror\b/.test(joined)) return 'bad'
  if (/blocked|skipped|retry|not change/.test(joined)) return 'warn'
  return 'ok'
}

// Unique, order-preserving action labels for the collapsed step summary chips.
function toolChips(step: AgentStepOut): string[] {
  const seen = new Set<string>()
  const out: string[] = []
  for (const a of step.actions) {
    const label = String(a.type ?? '')
      .replace(/Action$/, '')
      .replace(/([a-z])([A-Z])/g, '$1 $2')
      .toLowerCase()
    if (label && !seen.has(label)) {
      seen.add(label)
      out.push(label)
    }
  }
  return out
}

function fmtTime(iso: string): string {
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? '' : d.toLocaleTimeString([], { hour12: false })
}

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
  const activeSeq = stepList.length > 0 ? stepList[stepList.length - 1].seq : null

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

      <div className="grid items-start gap-5 lg:grid-cols-[minmax(0,1.05fr)_minmax(0,0.95fr)]">
        {/* LEFT -- step timeline */}
        <div className="flex min-w-0 flex-col gap-2">
        <div className="flex items-center gap-2">
          <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Steps</p>
          {stepList.length > 0 && (
            <span className="rounded-full border border-border bg-muted px-2 text-[11px] text-muted-foreground">
              {stepList.length} taken
            </span>
          )}
        </div>
        {stepList.length === 0 ? (
          <EmptyState title="No steps yet" description="The agent's per-step history will appear here." />
        ) : (
          <div className="flex flex-col gap-2.5">
            {stepList.map((step) => {
              // The newest step is "active" only while the run is still going.
              const isActive = isRunning && step.seq === activeSeq
              const status = stepStatus(step)
              const chips = toolChips(step)
              return (
                <details
                  key={step.seq}
                  open={isActive}
                  className={cn(
                    'group overflow-hidden rounded-lg border border-border bg-card shadow-sm',
                    isActive && 'border-accent/50 ring-1 ring-accent/30',
                  )}
                >
                  <summary className="flex cursor-pointer select-none items-center gap-3 px-3.5 py-3">
                    <StepNode n={step.step_number} status={status} active={isActive} />
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-sm font-medium">
                        {step.next_goal || step.evaluation_previous_goal || `Step ${step.step_number}`}
                      </span>
                      {chips.length > 0 && (
                        <span className="mt-1 flex flex-wrap gap-1.5">
                          {chips.map((c) => (
                            <span
                              key={c}
                              className="rounded border border-border bg-muted px-1.5 py-0.5 text-[11px] font-medium text-muted-foreground"
                            >
                              {c}
                            </span>
                          ))}
                        </span>
                      )}
                    </span>
                    <span className="flex shrink-0 items-center gap-3 text-[11px] tabular-nums text-muted-foreground">
                      {isActive ? (
                        <span className="flex items-center gap-1">
                          <Loader2 className="size-3 animate-spin" />
                          running
                        </span>
                      ) : (
                        <span>{fmtTime(step.created_at)}</span>
                      )}
                      <ChevronRight className="size-4 transition-transform group-open:rotate-90" />
                    </span>
                  </summary>

                  <div className="flex flex-col gap-3 border-t border-border px-3.5 pb-4 pt-3.5 pl-[52px]">
                    {step.thinking && (
                      <Reason icon={<Brain className="size-3.5 text-accent" />} label="Thinking" tone="accent">
                        {step.thinking}
                      </Reason>
                    )}
                    {step.evaluation_previous_goal && (
                      <Reason label="Evaluation">{step.evaluation_previous_goal}</Reason>
                    )}
                    {step.memory && <Reason label="Memory">{step.memory}</Reason>}
                    {step.next_goal && <Reason label="Next goal">{step.next_goal}</Reason>}

                    {step.actions.length > 0 && (
                      <div className="flex flex-col gap-1.5">
                        {step.actions.map((a, i) => (
                          <AgentActionCard key={i} action={a} />
                        ))}
                      </div>
                    )}

                    {step.action_results.length > 0 && (
                      <ul className="flex flex-col gap-1">
                        {step.action_results.map((r, i) => {
                          const bad = /\bfailed\b|\berror\b/i.test(r)
                          const warn = /blocked|skipped|retry|not change/i.test(r)
                          return (
                            <li key={i} className="flex items-start gap-1.5 text-xs text-muted-foreground">
                              {bad ? (
                                <CircleX className="mt-0.5 size-3.5 shrink-0 text-destructive" />
                              ) : warn ? (
                                <AlertTriangle className="mt-0.5 size-3.5 shrink-0 text-amber-500" />
                              ) : (
                                <CircleCheck className="mt-0.5 size-3.5 shrink-0 text-emerald-500" />
                              )}
                              <span>{r}</span>
                            </li>
                          )
                        })}
                      </ul>
                    )}
                  </div>
                </details>
              )
            })}
            {nextCursor && (
              <Button variant="outline" size="sm" onClick={loadMore} disabled={loadingMore} className="w-fit">
                {loadingMore ? 'Loading…' : 'Load more'}
              </Button>
            )}
          </div>
        )}
        </div>

        {/* RIGHT -- live browser while running, result once finished */}
        <div className="flex flex-col gap-2 lg:sticky lg:top-4">
          <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
            {isRunning ? 'Live browser' : 'Result'}
          </p>
          {isRunning && runId ? (
            <AgentLiveView runId={runId} />
          ) : run && run.status === 'failed' ? (
            <div className="flex items-start gap-2 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm">
              <Badge variant="destructive">failed</Badge>
              <span className="text-muted-foreground">{run.error ?? 'the run failed without an error message'}</span>
            </div>
          ) : run && run.status === 'completed' ? (
            run.result ? (
              <pre className="max-h-[32rem] overflow-auto whitespace-pre-wrap rounded-md border border-border bg-card p-3 text-xs shadow-sm">
                {JSON.stringify(run.result, null, 2)}
              </pre>
            ) : (
              <EmptyState title="No result returned" description="The run completed without a structured result." />
            )
          ) : (
            <EmptyState
              title="Nothing to show yet"
              description="Start a run to watch the agent's browser live, then see its result here."
            />
          )}
        </div>
      </div>

      <RecentRunsList runs={runs} />
    </div>
  )
}

function StepNode({ n, status, active }: { n: number; status: StepStatus; active: boolean }) {
  if (active) {
    return (
      <span className="flex size-7 shrink-0 items-center justify-center rounded-full bg-accent text-xs font-bold text-accent-foreground">
        {n}
      </span>
    )
  }
  const tone =
    status === 'bad'
      ? 'border-destructive/50 bg-destructive/10 text-destructive'
      : status === 'warn'
        ? 'border-amber-500/50 bg-amber-500/10 text-amber-600'
        : 'border-emerald-500/50 bg-emerald-500/10 text-emerald-600'
  return (
    <span
      className={cn(
        'flex size-7 shrink-0 items-center justify-center rounded-full border text-xs font-bold tabular-nums',
        tone,
      )}
    >
      {status === 'ok' ? <CheckCircle2 className="size-4" /> : n}
    </span>
  )
}

function Reason({
  label,
  icon,
  tone,
  children,
}: {
  label: string
  icon?: React.ReactNode
  tone?: 'accent'
  children: React.ReactNode
}) {
  return (
    <div className="grid grid-cols-[88px_1fr] items-start gap-2.5">
      <span
        className={cn(
          'flex items-center gap-1 pt-0.5 text-[11px] font-semibold uppercase tracking-wide',
          tone === 'accent' ? 'text-accent' : 'text-muted-foreground',
        )}
      >
        {icon}
        {label}
      </span>
      <span className="text-[13px] text-foreground/90">{children}</span>
    </div>
  )
}
