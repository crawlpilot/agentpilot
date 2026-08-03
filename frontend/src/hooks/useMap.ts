import { useMutation } from '@tanstack/react-query'
import { mapUrl } from '@/lib/api/map'
import { useAuth } from '@/lib/auth/AuthContext'
import type { MapRequest } from '@/lib/api/types'

export function useMap() {
  const { apiKey, tenant } = useAuth()
  return useMutation({
    mutationFn: (req: Omit<MapRequest, 'tenant'>) => mapUrl(apiKey!, { ...req, tenant: tenant! }),
  })
}
