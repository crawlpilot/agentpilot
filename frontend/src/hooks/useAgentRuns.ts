import { useEffect } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { agentRunEventsUrl, cancelAgentRun, createAgentRun, getAgentRunStatus, listAgentRuns } from '@/lib/api/agentRuns'
import { queryKeys } from '@/lib/query/queryClient'
import { useAuth } from '@/lib/auth/AuthContext'
import type { AgentRunCreateRequest, AgentRunOut, AgentRunStatusResponse, AgentStepOut, RunStatus } from '@/lib/api/types'

const TERMINAL_STATUSES: RunStatus[] = ['completed', 'failed', 'cancelled']

function mergeStep(prev: AgentRunStatusResponse | undefined, step: AgentStepOut): AgentRunStatusResponse {
  const base = prev ?? { success: true, data: null as unknown as AgentRunOut, steps: [], next: null }
  const others = base.steps.filter((s) => s.seq !== step.seq)
  return { ...base, steps: [...others, step].sort((a, b) => a.seq - b.seq) }
}

function setRun(prev: AgentRunStatusResponse | undefined, run: AgentRunOut): AgentRunStatusResponse {
  const base = prev ?? { success: true, data: run, steps: [], next: null }
  return { ...base, success: true, data: run }
}

/**
 * Streams a run over SSE, writing each `step`/`status`/`done` event straight
 * into the `agentRunStatus` query cache -- so the existing status-driven UI
 * updates the instant the worker persists a step, with no code change at the
 * read site. `useAgentRunStatus`'s poll stays mounted as a fallback (at a
 * relaxed interval) in case the stream drops. EventSource auto-reconnects on
 * transient errors; we only close it on `done` to avoid a reconnect storm
 * against a stream the server has intentionally ended.
 */
export function useAgentRunStream(runId: string | null) {
  const { apiKey } = useAuth()
  const queryClient = useQueryClient()
  useEffect(() => {
    if (!runId || !apiKey || typeof EventSource === 'undefined') return
    const key = queryKeys.agentRunStatus(runId)
    const es = new EventSource(agentRunEventsUrl(apiKey, runId))
    const onStep = (e: MessageEvent) =>
      queryClient.setQueryData<AgentRunStatusResponse>(key, (prev) => mergeStep(prev, JSON.parse(e.data)))
    const onStatus = (e: MessageEvent) =>
      queryClient.setQueryData<AgentRunStatusResponse>(key, (prev) => setRun(prev, JSON.parse(e.data)))
    const onDone = (e: MessageEvent) => {
      queryClient.setQueryData<AgentRunStatusResponse>(key, (prev) => setRun(prev, JSON.parse(e.data)))
      es.close()
    }
    es.addEventListener('step', onStep)
    es.addEventListener('status', onStatus)
    es.addEventListener('done', onDone)
    return () => es.close()
  }, [runId, apiKey, queryClient])
}

export function useCreateAgentRun() {
  const { apiKey, tenant } = useAuth()
  return useMutation({
    mutationFn: (req: Omit<AgentRunCreateRequest, 'tenant'>) =>
      createAgentRun(apiKey!, { ...req, tenant: tenant! }),
  })
}

export function useAgentRunStatus(runId: string | null) {
  const { apiKey } = useAuth()
  return useQuery({
    queryKey: queryKeys.agentRunStatus(runId ?? ''),
    queryFn: () => getAgentRunStatus(apiKey!, runId!),
    enabled: runId !== null,
    // Self-terminating poll -- stop once the run reaches a terminal status.
    // This is the *fallback* now that `useAgentRunStream` streams live updates
    // over SSE, so the interval is relaxed; it only matters if the stream drops.
    refetchInterval: (query) => {
      const status = query.state.data?.data.status
      return status && TERMINAL_STATUSES.includes(status) ? false : 5_000
    },
  })
}

export function useAgentRunsList() {
  const { apiKey } = useAuth()
  return useQuery({
    queryKey: queryKeys.agentRuns,
    queryFn: () => listAgentRuns(apiKey!),
    enabled: !!apiKey,
  })
}

export function useCancelAgentRun() {
  const { apiKey } = useAuth()
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (runId: string) => cancelAgentRun(apiKey!, runId),
    onSuccess: (_data, runId) =>
      queryClient.invalidateQueries({ queryKey: queryKeys.agentRunStatus(runId) }),
  })
}
