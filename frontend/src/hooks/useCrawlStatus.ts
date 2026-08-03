import { useQuery } from '@tanstack/react-query'
import { getCrawlStatus } from '@/lib/api/crawl'
import { queryKeys } from '@/lib/query/queryClient'
import { useAuth } from '@/lib/auth/AuthContext'
import type { CrawlJobStatus } from '@/lib/api/types'

const TERMINAL_STATUSES: CrawlJobStatus[] = ['completed', 'failed', 'cancelled']

export function useCrawlStatus(jobId: string | null) {
  const { apiKey } = useAuth()
  return useQuery({
    queryKey: queryKeys.crawlStatus(jobId ?? ''),
    queryFn: () => getCrawlStatus(apiKey!, jobId!),
    enabled: jobId !== null,
    // Poll while the job is still running; stop once it reaches a terminal
    // status -- a constant interval (like useSessionsList's) would keep
    // hammering the backend for a crawl that finished minutes ago.
    refetchInterval: (query) => {
      const status = query.state.data?.status
      return status && TERMINAL_STATUSES.includes(status) ? false : 2_000
    },
  })
}
