import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  codegenRecipe,
  createRecipe,
  getRecipe,
  getRecipeRun,
  getRunArtifacts,
  healRecipe,
  heartbeatAssist,
  listRecipes,
  listRecipeVersions,
  onboardRecipe,
  requestAssist,
  runRecipe,
  saveRecipeV2,
  submitAssist,
  updateRecipe,
} from '@/lib/api/recipes'
import { queryKeys } from '@/lib/query/queryClient'
import { useAuth } from '@/lib/auth/AuthContext'
import type {
  RecipeCodegenLanguage,
  RecipeCreateRequest,
  RecipeOnboardRequest,
  RecipeResolution,
  RecipeRunStatus,
  RecipeSaveRequest,
} from '@/lib/api/types'

// `needs_input` is deliberately NOT terminal: a parked run is mid-build,
// waiting for a person, and will carry on the moment one answers or its park
// expires. Treating it as finished would stop the poll exactly when the UI most
// needs to notice the run resuming.
const TERMINAL_STATUSES: RecipeRunStatus[] = ['completed', 'failed', 'cancelled']

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


/**
 * Build a recipe from a URL and a description of the wanted data.
 *
 * The run it queues is the long one -- an agent loop against a live site, then
 * a replay, then a judge. `useRecipeRun` is what watches it.
 */
export function useOnboardRecipe() {
  const { apiKey } = useAuth()
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (req: RecipeOnboardRequest) => onboardRecipe(apiKey!, req),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.recipes }),
  })
}

/**
 * Answer a parked run's questions.
 *
 * Invalidates the run so the UI sees it leave `needs_input` rather than waiting
 * out the poll interval -- the person just acted, and the page should say so.
 */
export function useSubmitAssist(recipeId: string, runId: string) {
  const { apiKey } = useAuth()
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (resolutions: RecipeResolution[]) =>
      submitAssist(apiKey!, recipeId, runId, { resolutions }),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: queryKeys.recipeRun(recipeId, runId) }),
  })
}

/**
 * Keep a parked run alive while somebody is working on it.
 *
 * The park holds a worker slot, a warm identity, a browser and a proxy pin, so
 * it is bounded — but a fixed bound drops the person doing the careful thing,
 * which is the reload, the recording, the pick and the look at what it read. A
 * heartbeat while the panel is open is what makes a longer ceiling safe rather
 * than merely longer.
 *
 * `enabled` gates it on the run actually being parked: extending a run that is
 * not waiting for anyone is a 409, and polling for one would be noise.
 */
export function useAssistHeartbeat(recipeId: string, runId: string, enabled: boolean) {
  const { apiKey } = useAuth()
  useQuery({
    queryKey: [...queryKeys.recipeRun(recipeId, runId), 'heartbeat'],
    queryFn: () => heartbeatAssist(apiKey!, recipeId, runId),
    enabled: Boolean(apiKey) && enabled,
    // Comfortably inside the shortest sensible park, and idempotent, so a missed
    // beat costs nothing and the next one covers it.
    refetchInterval: 60_000,
    refetchIntervalInBackground: true,
    // A 409 means the run stopped being parked, which the run poll will report
    // on its own. Retrying would just repeat the 409.
    retry: false,
  })
}

/**
 * Ask a running build to stop for a person before it finishes.
 *
 * For the failure that is not an empty field but a wrong one: on a page whose
 * own JSON carries a sponsored competitor under the same key names, every field
 * resolves and one of them is the wrong product. Nothing mechanical catches it.
 */
export function useRequestAssist(recipeId: string, runId: string) {
  const { apiKey } = useAuth()
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: () => requestAssist(apiKey!, recipeId, runId),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: queryKeys.recipeRun(recipeId, runId) }),
  })
}

/** What the build proposed for each field, and why each attempt was rejected. */
export function useRunArtifacts(recipeId: string, runId: string, kind?: string) {
  const { apiKey } = useAuth()
  return useQuery({
    queryKey: [...queryKeys.recipeRun(recipeId, runId), 'artifacts', kind ?? 'all'],
    queryFn: () => getRunArtifacts(apiKey!, recipeId, runId, kind),
    enabled: Boolean(apiKey) && Boolean(runId),
  })
}
