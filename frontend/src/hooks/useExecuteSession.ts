import { useMutation } from '@tanstack/react-query'
import { executeSession } from '@/lib/api/sessions'
import { useAuth } from '@/lib/auth/AuthContext'
import type { ActionIn } from '@/lib/api/types'

export function useExecuteSession() {
  const { apiKey } = useAuth()
  return useMutation({
    mutationFn: ({
      sessionId,
      actions,
      pageId,
    }: {
      sessionId: string
      actions: ActionIn[]
      pageId?: string | null
    }) => executeSession(apiKey!, sessionId, { actions, page_id: pageId }),
  })
}
