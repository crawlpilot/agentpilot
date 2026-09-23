import { apiRequest } from './client'
import type {
  RecipeAssistRequest,
  RecipeAssistResponse,
  RecipeRunArtifactsResponse,
  RecipeCodegenLanguage,
  RecipeCreateRequest,
  RecipeCreateResponse,
  RecipeGetResponse,
  RecipeJobQueuedResponse,
  RecipeJobRequest,
  RecipeJobResponse,
  RecipeJobsResponse,
  RecipeListResponse,
  RecipeOnboardRequest,
  RecipeOnboardResponse,
  RecipeRunQueuedResponse,
  RecipeRunResponse,
  RecipeSaveRequest,
  RecipeSaveResponse,
  RecipeVersionsResponse,
  TemplatesResponse,
} from './types'

export function createRecipe(token: string, req: RecipeCreateRequest) {
  return apiRequest<RecipeCreateResponse>('/v1/recipes', { method: 'POST', body: req, token })
}

/**
 * Save an authored v2 document as a NEW recipe.
 *
 * Not `createRecipe`: that posts a name and a URL and starts an agent *build*.
 * This stores a finished document that was authored and previewed against a
 * live page, without queueing anything.
 */
export function saveRecipeV2(token: string, req: RecipeSaveRequest) {
  return apiRequest<RecipeSaveResponse>('/v1/recipes/v2', { method: 'POST', body: req, token })
}

/** Replace a recipe's document, as a new append-only version. */
export function updateRecipe(token: string, recipeId: string, req: RecipeSaveRequest) {
  return apiRequest<RecipeSaveResponse>(`/v1/recipes/${recipeId}`, {
    method: 'PUT',
    body: req,
    token,
  })
}

/**
 * Build a recipe from a URL and a description of the wanted data.
 *
 * Not `createRecipe`, which starts a v1 build whose output has no v2 document
 * and so can never run in the marketplace or be opened in the studio. This is
 * the one that produces the document.
 *
 * Queued, not synchronous: onboarding drives a real browser through an agent
 * loop against a live site, which is minutes. Poll the run.
 */
export function onboardRecipe(token: string, req: RecipeOnboardRequest) {
  return apiRequest<RecipeOnboardResponse>('/v1/recipes/onboard', {
    method: 'POST',
    body: req,
    token,
  })
}

/**
 * Answer what a parked onboarding run is waiting for.
 *
 * The run's worker is still holding a live browser session on the page it got
 * stuck on, which is why an answer can be an element pick rather than a
 * description. Accepting flips the run back to `running`.
 */
export function submitAssist(
  token: string,
  recipeId: string,
  runId: string,
  req: RecipeAssistRequest,
) {
  return apiRequest<RecipeAssistResponse>(`/v1/recipes/${recipeId}/runs/${runId}/assist`, {
    method: 'POST',
    body: req,
    token,
  })
}

/**
 * Say that somebody is still working on a parked run.
 *
 * The park is bounded because it holds a worker slot, a warm identity, a browser
 * and a proxy pin — but a fixed bound drops the person doing the careful thing,
 * which is the reload, the recording, the pick and the look at what it read.
 * Called on a timer while the assist panel is open.
 */
export function heartbeatAssist(token: string, recipeId: string, runId: string) {
  return apiRequest<RecipeAssistResponse>(
    `/v1/recipes/${recipeId}/runs/${runId}/assist/heartbeat`,
    { method: 'POST', token },
  )
}

/**
 * Ask a running build to stop for a person before it finishes.
 *
 * Takes effect at the build's next park point, with the browser session still
 * open on the page. Until this existed, taking over meant waiting for the build
 * to give up — so a build that bound every field, correctly or not, finished and
 * saved without ever offering.
 */
export function requestAssist(token: string, recipeId: string, runId: string) {
  return apiRequest<RecipeAssistResponse>(
    `/v1/recipes/${recipeId}/runs/${runId}/assist/request`,
    { method: 'POST', token },
  )
}

/**
 * What the build proposed for each field, and why each attempt was rejected.
 *
 * Its own endpoint rather than part of the run poll: a trace carries every
 * locator tried plus a sample of what each read, and the run is polled every few
 * seconds while a build takes minutes.
 */
export function getRunArtifacts(
  token: string,
  recipeId: string,
  runId: string,
  kind?: string,
) {
  return apiRequest<RecipeRunArtifactsResponse>(
    `/v1/recipes/${recipeId}/runs/${runId}/artifacts`,
    { token, query: kind ? { kind } : {} },
  )
}

/** The scraper marketplace: recipes published as prebuilt templates. */
export function listTemplates(token: string, params: { domain?: string; page_type?: string } = {}) {
  return apiRequest<TemplatesResponse>('/v1/recipes/templates', { token, query: params })
}

export function listRecipes(token: string, after?: string, limit?: number) {
  return apiRequest<RecipeListResponse>('/v1/recipes', {
    token,
    query: { after, limit: limit?.toString() },
  })
}

export function getRecipe(token: string, recipeId: string) {
  return apiRequest<RecipeGetResponse>(`/v1/recipes/${recipeId}`, { token })
}

export function runRecipe(token: string, recipeId: string) {
  return apiRequest<RecipeRunQueuedResponse>(`/v1/recipes/${recipeId}/run`, { method: 'POST', token })
}

export function healRecipe(token: string, recipeId: string) {
  return apiRequest<RecipeRunQueuedResponse>(`/v1/recipes/${recipeId}/heal`, { method: 'POST', token })
}

export function codegenRecipe(token: string, recipeId: string, language: RecipeCodegenLanguage) {
  return apiRequest<RecipeRunQueuedResponse>(`/v1/recipes/${recipeId}/codegen`, {
    method: 'POST',
    body: { language },
    token,
  })
}

export function listRecipeVersions(token: string, recipeId: string) {
  return apiRequest<RecipeVersionsResponse>(`/v1/recipes/${recipeId}/versions`, { token })
}

export function getRecipeRun(token: string, recipeId: string, runId: string) {
  return apiRequest<RecipeRunResponse>(`/v1/recipes/${recipeId}/runs/${runId}`, { token })
}

// --- extraction jobs: a marketplace recipe applied to submitted urls ---

/**
 * Apply a recipe to the caller's own URLs. One queued run per URL.
 *
 * Not `runRecipe`: that queues the recipe's *own* scheduled replay against the
 * URL pattern it was built for, and reports against the recipe's health. This
 * takes the URLs from the caller and leaves health alone.
 */
export function submitRecipeJob(token: string, recipeId: string, req: RecipeJobRequest) {
  return apiRequest<RecipeJobQueuedResponse>(`/v1/recipes/${recipeId}/jobs`, {
    method: 'POST',
    body: req,
    token,
  })
}

export function getRecipeJob(token: string, recipeId: string, jobId: string) {
  return apiRequest<RecipeJobResponse>(`/v1/recipes/${recipeId}/jobs/${jobId}`, { token })
}

export function listRecipeJobs(token: string, recipeId: string) {
  return apiRequest<RecipeJobsResponse>(`/v1/recipes/${recipeId}/jobs`, { token })
}
