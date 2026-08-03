import { apiRequest } from './client'
import type { MapRequest, MapResponse } from './types'

export function mapUrl(token: string, req: MapRequest) {
  return apiRequest<MapResponse>('/v1/map', { method: 'POST', body: req, token })
}
