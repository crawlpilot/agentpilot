import { apiRequest } from './client'
import type {
  RecipeCodegenLanguage,
  RecipeCreateRequest,
  RecipeCreateResponse,
  RecipeGetResponse,
  RecipeJobQueuedResponse,
  RecipeJobRequest,
  RecipeJobResponse,
  RecipeJobsResponse,
  RecipeListResponse,
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
