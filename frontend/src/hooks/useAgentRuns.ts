import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { cancelAgentRun, createAgentRun, getAgentRunStatus } from '@/lib/api/agentRuns'
import { queryKeys } from '@/lib/query/queryClient'
import { useAuth } from '@/lib/auth/AuthContext'
import type { AgentRunCreateRequest, RunStatus } from '@/lib/api/types'

const TERMINAL_STATUSES: RunStatus[] = ['completed', 'failed', 'cancelled']

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
    // Self-terminating poll -- stop once the run reaches a terminal status,
    // same shape as useCrawlStatus.
    refetchInterval: (query) => {
      const status = query.state.data?.data.status
      return status && TERMINAL_STATUSES.includes(status) ? false : 2_000
    },
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
