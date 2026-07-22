import { apiRequest } from './client'
import type { ApiKeyCreateOut, ApiKeyCreateRequest, ApiKeyListOut } from './types'

// Every call here takes the admin token, never a tenant api key -- this is
// the control-plane surface `require_admin` gates (see the backend's
// `auth_deps.py`), separate from the tenant-facing session/execute calls.

export function createApiKey(adminToken: string, req: ApiKeyCreateRequest) {
  return apiRequest<ApiKeyCreateOut>('/v1/api-keys', { method: 'POST', body: req, token: adminToken })
}

export function listApiKeys(adminToken: string, tenant: string) {
  return apiRequest<ApiKeyListOut>('/v1/api-keys', { token: adminToken, query: { tenant } })
}

export function revokeApiKey(adminToken: string, keyId: string) {
  return apiRequest<{ success: boolean }>(`/v1/api-keys/${keyId}`, {
    method: 'DELETE',
    token: adminToken,
  })
}
