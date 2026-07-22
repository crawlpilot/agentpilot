import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { createApiKey, listApiKeys, revokeApiKey } from '@/lib/api/apiKeys'
import { queryKeys } from '@/lib/query/queryClient'
import { useAuth } from '@/lib/auth/AuthContext'

export function useApiKeysList(tenant: string) {
  const { adminToken } = useAuth()
  return useQuery({
    queryKey: queryKeys.apiKeys(tenant),
    queryFn: () => listApiKeys(adminToken!, tenant),
    enabled: Boolean(adminToken) && Boolean(tenant),
  })
}

export function useCreateApiKey(tenant: string) {
  const { adminToken } = useAuth()
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (name: string) => createApiKey(adminToken!, { tenant, name }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.apiKeys(tenant) }),
  })
}

export function useRevokeApiKey(tenant: string) {
  const { adminToken } = useAuth()
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (keyId: string) => revokeApiKey(adminToken!, keyId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.apiKeys(tenant) }),
  })
}
