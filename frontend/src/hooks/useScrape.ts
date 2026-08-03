import { useMutation } from '@tanstack/react-query'
import { scrapeUrl } from '@/lib/api/scrape'
import { useAuth } from '@/lib/auth/AuthContext'
import type { ScrapeRequest } from '@/lib/api/types'

export function useScrape() {
  const { apiKey, tenant } = useAuth()
  return useMutation({
    mutationFn: (req: Omit<ScrapeRequest, 'tenant'>) => scrapeUrl(apiKey!, { ...req, tenant: tenant! }),
  })
}
