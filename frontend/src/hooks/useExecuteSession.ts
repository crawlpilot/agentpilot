import { useMutation } from '@tanstack/react-query'
import { executeSession } from '@/lib/api/sessions'
import { useAuth } from '@/lib/auth/AuthContext'
import type { ActionIn } from '@/lib/api/types'

export function useExecuteSession() {
  const { apiKey } = useAuth()
  return useMutation({
    mutationFn: ({ sessionId, actions }: { sessionId: string; actions: ActionIn[] }) =>
      executeSession(apiKey!, sessionId, { actions }),
  })
}
