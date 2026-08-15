import { apiRequest } from './client'
import { API_BASE_URL } from '@/lib/config'
import type {
  AgentRunCreateRequest,
  AgentRunCreateResponse,
  AgentRunListResponse,
  AgentRunStatusResponse,
} from './types'

export function createAgentRun(token: string, req: AgentRunCreateRequest) {
  return apiRequest<AgentRunCreateResponse>('/v1/agent/runs', { method: 'POST', body: req, token })
}

export function listAgentRuns(token: string, after?: string, limit?: number) {
  return apiRequest<AgentRunListResponse>('/v1/agent/runs', {
    token,
    query: { after, limit: limit?.toString() },
  })
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

/** SSE endpoint for a run -- EventSource can't set headers, so the key rides
 * on the query string (same pattern as the live-view socket). */
export function agentRunEventsUrl(token: string, runId: string): string {
  return `${API_BASE_URL}/v1/agent/runs/${runId}/events?api_key=${encodeURIComponent(token)}`
}

/** Direct <img src> URL for a step's screenshot; key on the query string
 * because an image request carries no Authorization header. */
export function agentStepScreenshotUrl(token: string, runId: string, seq: number): string {
  return `${API_BASE_URL}/v1/agent/runs/${runId}/steps/${seq}/screenshot?api_key=${encodeURIComponent(token)}`
}
