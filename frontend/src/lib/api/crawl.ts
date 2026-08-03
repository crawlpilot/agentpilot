import { apiRequest } from './client'
import type { CrawlCreateResponse, CrawlRequest, CrawlStatusResponse } from './types'

export function createCrawl(token: string, req: CrawlRequest) {
  return apiRequest<CrawlCreateResponse>('/v1/crawl', { method: 'POST', body: req, token })
}

export function getCrawlStatus(token: string, jobId: string, after?: string, limit?: number) {
  return apiRequest<CrawlStatusResponse>(`/v1/crawl/${jobId}`, {
    token,
    query: { after, limit: limit?.toString() },
  })
}

export function cancelCrawl(token: string, jobId: string) {
  return apiRequest<{ success: boolean }>(`/v1/crawl/${jobId}`, { method: 'DELETE', token })
}
