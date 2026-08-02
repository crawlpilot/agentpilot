import { useQuery } from '@tanstack/react-query'
import { getCdpInfo } from '@/lib/api/sessions'
import { queryKeys } from '@/lib/query/queryClient'
import { useAuth } from '@/lib/auth/AuthContext'

// `enabled` is caller-controlled (not just `isAuthed`) so this only fires
// once the connect dialog is actually open -- no reason to hit the discovery
// endpoint, which round-trips to the session's own worker, for every row in
// the sessions table on every render.
export function useCdpInfo(sessionId: string, enabled: boolean) {
  const { apiKey, isAuthed } = useAuth()

  return useQuery({
    queryKey: queryKeys.cdpInfo(sessionId),
    queryFn: () => getCdpInfo(apiKey!, sessionId),
    enabled: isAuthed && enabled,
    staleTime: 30_000,
  })
}
