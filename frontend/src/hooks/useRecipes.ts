import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  codegenRecipe,
  createRecipe,
  getRecipe,
  getRecipeRun,
  healRecipe,
  listRecipes,
  listRecipeVersions,
  runRecipe,
  saveRecipeV2,
  updateRecipe,
} from '@/lib/api/recipes'
import { queryKeys } from '@/lib/query/queryClient'
import { useAuth } from '@/lib/auth/AuthContext'
import type {
  RecipeCodegenLanguage,
  RecipeCreateRequest,
  RecipeSaveRequest,
  RunStatus,
} from '@/lib/api/types'

const TERMINAL_STATUSES: RunStatus[] = ['completed', 'failed', 'cancelled']

export function useRecipesList() {
  const { apiKey, isAuthed } = useAuth()
  return useQuery({
    queryKey: queryKeys.recipes,
    queryFn: () => listRecipes(apiKey!),
    enabled: isAuthed,
    // Health/last-run can change from an out-of-band scheduled replay, so
    // poll like the sessions list rather than fetch-once.
    refetchInterval: 5_000,
  })
}

export function useRecipe(recipeId: string) {
  const { apiKey, isAuthed } = useAuth()
  return useQuery({
    queryKey: queryKeys.recipe(recipeId),
    queryFn: () => getRecipe(apiKey!, recipeId),
    // An empty id would GET `/v1/recipes/`, which is the *list* route -- a
    // request that succeeds and returns the wrong shape. The studio's "new
    // recipe" route has no id, so guard it here rather than at each caller.
    enabled: isAuthed && recipeId !== '',
    // A recipe is a persistent entity, not a terminal job -- keep polling so
    // a build/heal finishing in the worker is eventually reflected here.
    refetchInterval: 5_000,
  })
}

export function useRecipeVersions(recipeId: string) {
  const { apiKey, isAuthed } = useAuth()
  return useQuery({
    queryKey: queryKeys.recipeVersions(recipeId),
    queryFn: () => listRecipeVersions(apiKey!, recipeId),
    enabled: isAuthed,
    refetchInterval: 5_000,
  })
}

export function useRecipeRun(recipeId: string, runId: string | null) {
  const { apiKey } = useAuth()
  return useQuery({
    queryKey: queryKeys.recipeRun(recipeId, runId ?? ''),
    queryFn: () => getRecipeRun(apiKey!, recipeId, runId!),
    enabled: runId !== null,
    refetchInterval: (query) => {
      const status = query.state.data?.data.status
      return status && TERMINAL_STATUSES.includes(status) ? false : 2_000
    },
  })
}

export function useCreateRecipe() {
  const { apiKey, tenant } = useAuth()
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (req: Omit<RecipeCreateRequest, 'tenant'>) => createRecipe(apiKey!, { ...req, tenant: tenant! }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.recipes }),
  })
}

export function useRunRecipe(recipeId: string) {
  const { apiKey } = useAuth()
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: () => runRecipe(apiKey!, recipeId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.recipe(recipeId) }),
  })
}

export function useHealRecipe(recipeId: string) {
  const { apiKey } = useAuth()
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: () => healRecipe(apiKey!, recipeId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.recipe(recipeId) }),
  })
}

export function useCodegenRecipe(recipeId: string) {
  const { apiKey } = useAuth()
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (language: RecipeCodegenLanguage) => codegenRecipe(apiKey!, recipeId, language),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.recipe(recipeId) }),
  })
}


/**
 * Save an authored v2 document -- new recipe, or a new version of one.
 *
 * The branch is on `recipeId` rather than on two hooks because the wizard does
 * not know which it is doing until the first save returns an id.
 */
export function useSaveRecipe() {
  const { apiKey } = useAuth()
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ recipeId, ...req }: RecipeSaveRequest & { recipeId?: string | null }) =>
      recipeId ? updateRecipe(apiKey!, recipeId, req) : saveRecipeV2(apiKey!, req),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.recipes })
    },
  })
}
