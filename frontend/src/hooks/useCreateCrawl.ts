import { useMutation } from '@tanstack/react-query'
import { createCrawl } from '@/lib/api/crawl'
import { useAuth } from '@/lib/auth/AuthContext'
import type { CrawlRequest } from '@/lib/api/types'

export function useCreateCrawl() {
  const { apiKey, tenant } = useAuth()
  return useMutation({
    mutationFn: (req: Omit<CrawlRequest, 'tenant'>) => createCrawl(apiKey!, { ...req, tenant: tenant! }),
  })
}
