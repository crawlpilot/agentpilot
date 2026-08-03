import { apiRequest } from './client'
import type { ScrapeRequest, ScrapeResponse } from './types'

export function scrapeUrl(token: string, req: ScrapeRequest) {
  return apiRequest<ScrapeResponse>('/v1/scrape', { method: 'POST', body: req, token })
}
