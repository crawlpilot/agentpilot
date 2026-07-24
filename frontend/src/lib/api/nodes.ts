import { apiRequest } from './client'
import type { NodeListOut } from './types'

// Admin-gated, same as apiKeys.ts -- fleet visibility is an operator
// concern (GET /v1/nodes requires require_admin), not a per-tenant call.

export function listNodes(adminToken: string) {
  return apiRequest<NodeListOut>('/v1/nodes', { token: adminToken })
}
