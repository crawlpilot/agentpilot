import { useQuery } from '@tanstack/react-query'
import { listNodes } from '@/lib/api/nodes'
import { queryKeys } from '@/lib/query/queryClient'
import { useAuth } from '@/lib/auth/AuthContext'

export function useNodesList() {
  const { adminToken } = useAuth()

  return useQuery({
    queryKey: queryKeys.nodes,
    queryFn: () => listNodes(adminToken!),
    enabled: Boolean(adminToken),
    refetchInterval: 5_000,
  })
}
