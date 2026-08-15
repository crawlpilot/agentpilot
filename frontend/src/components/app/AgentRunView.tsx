import { useEffect, useState } from 'react'
import {
  AlertTriangle,
  Brain,
  CheckCircle2,
  ChevronRight,
  CircleCheck,
  CircleX,
  Loader2,
} from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Badge, type BadgeProps } from '@/components/ui/badge'
import { EmptyState } from '@/components/app/EmptyState'
import { AgentActionCard } from '@/components/app/AgentActionCard'
import { AgentLiveView } from '@/components/app/AgentLiveView'
import { useAgentRunStatus, useAgentRunStream, useCancelAgentRun } from '@/hooks/useAgentRuns'
import { useAuth } from '@/lib/auth/AuthContext'
import { useToast } from '@/components/ui/toast'
import { getAgentRunStatus, agentStepScreenshotUrl } from '@/lib/api/agentRuns'
import { cn } from '@/lib/utils'
import type { AgentStepOut, RunStatus } from '@/lib/api/types'

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

function fmtDuration(ms: number): string {
  if (ms < 1000) return `${ms}ms`
  const s = ms / 1000
  if (s < 60) return `${s.toFixed(1)}s`
  const m = Math.floor(s / 60)
  return `${m}:${String(Math.round(s % 60)).padStart(2, '0')}`
}

function fmtTokens(n: number): string {
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n)
}

export function statusVariant(status: RunStatus): NonNullable<BadgeProps['variant']> {
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

/**
 * The full viewer for a single agent run: status header + telemetry, a typed
 * step timeline, the live browser (while running) or result (once finished).
 * Self-contained given a `runId` -- owns its own status poll + SSE stream +
 * step accumulation -- so both the Playground tab and the shareable run-detail
 * page render the same thing. `showCancel` hides the cancel button on the
 * read-only detail page.
 */
export function AgentRunView({ runId, showCancel = true }: { runId: string; showCancel?: boolean }) {
  const [steps, setSteps] = useState<Record<number, AgentStepOut>>({})
  const [nextCursor, setNextCursor] = useState<string | null>(null)
  const [paginated, setPaginated] = useState(false)
  const [loadingMore, setLoadingMore] = useState(false)

  const { apiKey } = useAuth()
  const { toast } = useToast()
  const cancelRun = useCancelAgentRun()
  const { data: status } = useAgentRunStatus(runId)
  useAgentRunStream(runId)

  // Reset accumulated steps when switching to a different run.
  useEffect(() => {
    setSteps({})
    setNextCursor(null)
    setPaginated(false)
  }, [runId])

  useEffect(() => {
    if (!status) return
    setSteps((prev) => {
      const next = { ...prev }
      for (const step of status.steps) next[step.seq] = step
      return next
    })
    if (!paginated) setNextCursor(status.next)
  }, [status, paginated])

  function handleCancel() {
    cancelRun.mutate(runId, {
      onSuccess: () => toast({ title: 'Agent run cancelled' }),
      onError: (err) => toast({ title: 'Cancel failed', description: err.message, variant: 'destructive' }),
    })
  }

  async function loadMore() {
    if (!nextCursor || !apiKey) return
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
  const totalIn = stepList.reduce((s, x) => s + (x.input_tokens ?? 0), 0)
  const totalOut = stepList.reduce((s, x) => s + (x.output_tokens ?? 0), 0)
  const timedSteps = stepList.filter((x) => x.duration_ms != null)
  const totalMs = timedSteps.reduce((s, x) => s + (x.duration_ms ?? 0), 0)
  const avgMs = timedSteps.length ? Math.round(totalMs / timedSteps.length) : 0
  const hasTelemetry = totalIn + totalOut > 0 || timedSteps.length > 0

  return (
    <div className="flex flex-col gap-6">
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
            {showCancel && (
              <Button size="sm" variant="outline" disabled={!isRunning || cancelRun.isPending} onClick={handleCancel}>
                {cancelRun.isPending ? 'Cancelling…' : 'Cancel'}
              </Button>
            )}
          </div>
          <div className="h-2 w-full overflow-hidden rounded-full bg-muted">
            <div className="h-full rounded-full bg-accent transition-all" style={{ width: `${pct}%` }} />
          </div>
          {hasTelemetry && (
            <div className="mt-1 flex flex-wrap gap-x-6 gap-y-2 text-xs">
              <Stat label="Tokens" value={`${fmtTokens(totalIn)} in · ${fmtTokens(totalOut)} out`} />
              <Stat label="LLM time" value={fmtDuration(totalMs)} />
              <Stat label="Avg step" value={avgMs ? fmtDuration(avgMs) : '—'} />
            </div>
          )}
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
                const st = stepStatus(step)
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
                      <StepNode n={step.step_number} status={st} active={isActive} />
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
                          <>
                            {step.duration_ms != null && <span>{fmtDuration(step.duration_ms)}</span>}
                            {(step.input_tokens ?? 0) + (step.output_tokens ?? 0) > 0 && (
                              <span>{fmtTokens((step.input_tokens ?? 0) + (step.output_tokens ?? 0))} tok</span>
                            )}
                            {step.duration_ms == null && <span>{fmtTime(step.created_at)}</span>}
                          </>
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

                      {step.has_screenshot && apiKey && (
                        <a
                          href={agentStepScreenshotUrl(apiKey, runId, step.seq)}
                          target="_blank"
                          rel="noreferrer"
                          className="w-fit"
                          title="Open full screenshot"
                        >
                          <img
                            src={agentStepScreenshotUrl(apiKey, runId, step.seq)}
                            alt={`Step ${step.step_number} screenshot`}
                            loading="lazy"
                            className="max-h-40 rounded-md border border-border object-cover object-top"
                          />
                        </a>
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
          {isRunning ? (
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
    </div>
  )
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col">
      <span className="text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">{label}</span>
      <span className="tabular-nums font-medium">{value}</span>
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
