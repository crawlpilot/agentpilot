import { apiRequest } from './client'
import type {
  RecipeCodegenLanguage,
  RecipeCreateRequest,
  RecipeCreateResponse,
  RecipeGetResponse,
  RecipeListResponse,
  RecipeRunQueuedResponse,
  RecipeRunResponse,
  RecipeVersionsResponse,
} from './types'

export function createRecipe(token: string, req: RecipeCreateRequest) {
  return apiRequest<RecipeCreateResponse>('/v1/recipes', { method: 'POST', body: req, token })
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
