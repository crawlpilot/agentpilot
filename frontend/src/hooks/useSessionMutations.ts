import { useMutation, useQueryClient } from '@tanstack/react-query'
import { openSession, releaseSession } from '@/lib/api/sessions'
import { queryKeys } from '@/lib/query/queryClient'
import { useAuth } from '@/lib/auth/AuthContext'
import type { SessionOpenRequest } from '@/lib/api/types'

export function useOpenSession() {
  const { apiKey, tenant } = useAuth()
  const queryClient = useQueryClient()

  return useMutation({
    mutationFn: (req: Omit<SessionOpenRequest, 'tenant'>) =>
      openSession(apiKey!, { ...req, tenant: tenant! }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.sessions })
    },
  })
}

export function useReleaseSession() {
  const { apiKey } = useAuth()
  const queryClient = useQueryClient()

  return useMutation({
    mutationFn: (sessionId: string) => releaseSession(apiKey!, sessionId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.sessions })
    },
  })
}
