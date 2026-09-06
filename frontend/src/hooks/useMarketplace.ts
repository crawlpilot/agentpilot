import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { getRecipeJob, listRecipeJobs, listTemplates, submitRecipeJob } from '@/lib/api/recipes'
import { queryKeys } from '@/lib/query/queryClient'
import { useAuth } from '@/lib/auth/AuthContext'
import type { RecipeJobRequest } from '@/lib/api/types'

/**
 * The marketplace's two halves.
 *
 * `useTemplates` is browse: recipes their authors chose to publish, which is a
 * different list from `useRecipesList` (your own recipes, published or not).
 * The rest is use: submitting URLs to one, and watching the batch.
 */

export function useTemplates(domain?: string, pageType?: string) {
  const { apiKey, isAuthed } = useAuth()
  return useQuery({
    queryKey: queryKeys.templates(domain, pageType),
    queryFn: () => listTemplates(apiKey!, { domain, page_type: pageType }),
    enabled: isAuthed,
    // A catalogue changes when someone publishes, which is rare. No polling --
    // unlike a run, nothing here becomes stale while you look at it.
    staleTime: 30_000,
  })
}

export function useRecipeJobs(recipeId: string) {
  const { apiKey, isAuthed } = useAuth()
  return useQuery({
    queryKey: queryKeys.recipeJobs(recipeId),
    queryFn: () => listRecipeJobs(apiKey!, recipeId),
    enabled: isAuthed && recipeId !== '',
    refetchInterval: 5_000,
  })
}

export function useRecipeJob(recipeId: string, jobId: string) {
  const { apiKey, isAuthed } = useAuth()
  return useQuery({
    queryKey: queryKeys.recipeJob(recipeId, jobId),
    queryFn: () => getRecipeJob(apiKey!, recipeId, jobId),
    enabled: isAuthed && recipeId !== '' && jobId !== '',
    // Poll only while work is outstanding. A finished job is immutable, and a
    // batch of 500 URLs would otherwise be re-fetched every two seconds
    // forever once it had nothing left to say.
    refetchInterval: (query) => (query.state.data?.job.status === 'running' ? 2_000 : false),
  })
}

export function useSubmitJob(recipeId: string) {
  const { apiKey } = useAuth()
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (req: RecipeJobRequest) => submitRecipeJob(apiKey!, recipeId, req),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: queryKeys.recipeJobs(recipeId) }),
  })
}
