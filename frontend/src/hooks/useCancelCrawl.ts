import { useMutation, useQueryClient } from '@tanstack/react-query'
import { cancelCrawl } from '@/lib/api/crawl'
import { queryKeys } from '@/lib/query/queryClient'
import { useAuth } from '@/lib/auth/AuthContext'

export function useCancelCrawl() {
  const { apiKey } = useAuth()
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (jobId: string) => cancelCrawl(apiKey!, jobId),
    onSuccess: (_data, jobId) => queryClient.invalidateQueries({ queryKey: queryKeys.crawlStatus(jobId) }),
  })
}
