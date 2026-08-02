import { QueryClient } from '@tanstack/react-query'

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      refetchOnWindowFocus: false,
    },
  },
})

export const queryKeys = {
  health: ['health'] as const,
  sessions: ['sessions'] as const,
  apiKeys: (tenant: string) => ['api-keys', tenant] as const,
  nodes: ['nodes'] as const,
  cdpInfo: (sessionId: string) => ['cdp-info', sessionId] as const,
}
