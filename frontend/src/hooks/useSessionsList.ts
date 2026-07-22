import { useQuery } from '@tanstack/react-query'
import { listSessions } from '@/lib/api/sessions'
import { queryKeys } from '@/lib/query/queryClient'
import { useAuth } from '@/lib/auth/AuthContext'

export function useSessionsList() {
  const { apiKey, isAuthed } = useAuth()

  return useQuery({
    queryKey: queryKeys.sessions,
    queryFn: () => listSessions(apiKey!),
    enabled: isAuthed,
    refetchInterval: 5_000,
  })
}
