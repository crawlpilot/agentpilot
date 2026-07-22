import { useQuery } from '@tanstack/react-query'
import { queryKeys } from '@/lib/query/queryClient'

export function useHealth() {
  return useQuery({
    queryKey: queryKeys.health,
    queryFn: async () => {
      const res = await fetch('/healthz')
      if (!res.ok) throw new Error('unhealthy')
      return (await res.json()) as { status: string }
    },
    refetchInterval: 10_000,
    retry: false,
  })
}
