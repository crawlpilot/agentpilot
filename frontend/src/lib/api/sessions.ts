import { apiRequest } from './client'
import type {
  ActionResult,
  CdpDiscoveryOut,
  ExecuteRequest,
  SessionListOut,
  SessionOpenRequest,
  SessionOpenResponse,
} from './types'

export function openSession(token: string, req: SessionOpenRequest) {
  return apiRequest<SessionOpenResponse>('/v1/sessions', { method: 'POST', body: req, token })
}

export function listSessions(token: string) {
  return apiRequest<SessionListOut>('/v1/sessions', { token })
}

export function executeSession(token: string, sessionId: string, req: ExecuteRequest) {
  return apiRequest<ActionResult>(`/v1/sessions/${sessionId}/execute`, {
    method: 'POST',
    body: req,
    token,
  })
}

export function getCdpInfo(token: string, sessionId: string) {
  return apiRequest<CdpDiscoveryOut>(`/v1/sessions/${sessionId}/cdp/json/version`, { token })
}

export function releaseSession(token: string, sessionId: string) {
  return apiRequest<{ success: boolean; state: string }>(`/v1/sessions/${sessionId}`, {
    method: 'DELETE',
    token,
  })
}
