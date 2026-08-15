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
  crawlStatus: (jobId: string) => ['crawl-status', jobId] as const,
  agentRunStatus: (runId: string) => ['agent-run-status', runId] as const,
  agentRuns: ['agent-runs'] as const,
  recipes: ['recipes'] as const,
  recipe: (id: string) => ['recipe', id] as const,
  recipeVersions: (id: string) => ['recipe-versions', id] as const,
  recipeRun: (recipeId: string, runId: string) => ['recipe-run', recipeId, runId] as const,
}
