import { apiRequest } from './client'
import type { AgentRunCreateRequest, AgentRunCreateResponse, AgentRunStatusResponse } from './types'

export function createAgentRun(token: string, req: AgentRunCreateRequest) {
  return apiRequest<AgentRunCreateResponse>('/v1/agent/runs', { method: 'POST', body: req, token })
}

export function getAgentRunStatus(token: string, runId: string, after?: string, limit?: number) {
  return apiRequest<AgentRunStatusResponse>(`/v1/agent/runs/${runId}`, {
    token,
    query: { after, limit: limit?.toString() },
  })
}

export function cancelAgentRun(token: string, runId: string) {
  return apiRequest<{ success: boolean }>(`/v1/agent/runs/${runId}`, { method: 'DELETE', token })
}
