import { useNavigate } from 'react-router-dom'
import { BookOpen } from 'lucide-react'
import { useRecipesList } from '@/hooks/useRecipes'
import { RecipesTable } from '@/components/app/RecipesTable'
import { RecipeCreateDialog } from '@/components/app/RecipeCreateDialog'
import { EmptyState } from '@/components/app/EmptyState'
import { Skeleton } from '@/components/ui/skeleton'
import { useAuth } from '@/lib/auth/AuthContext'

export function RecipesListPage() {
  const navigate = useNavigate()
  const { isAuthed } = useAuth()
  const { data, isLoading, isError } = useRecipesList()
  const recipes = data?.recipes ?? []

  function handleCreated(recipeId: string, buildRunId: string) {
    navigate(`/recipes/${recipeId}?runId=${buildRunId}&kind=build`)
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold">Recipes</h1>
          <p className="text-sm text-muted-foreground">
            Reusable, self-healing extraction recipes -- built once, replayed on demand or on a schedule.
          </p>
        </div>
        {isAuthed && <RecipeCreateDialog onCreated={handleCreated} />}
      </div>

      {!isAuthed ? (
        <EmptyState
          icon={<BookOpen className="size-8" />}
          title="Tenant API key required"
          description="Sign in with a tenant API key to manage recipes -- an admin token alone doesn't carry tenant scope."
        />
      ) : isLoading ? (
        <div className="flex flex-col gap-2">
          {[0, 1, 2].map((i) => (
            <Skeleton key={i} className="h-12" />
          ))}
        </div>
      ) : isError ? (
        <EmptyState title="Couldn't load recipes" description="Check that the gateway is reachable and your api key is valid." />
      ) : recipes.length === 0 ? (
        <EmptyState
          icon={<BookOpen className="size-8" />}
          title="No recipes yet"
          description="Create one to build a reusable extraction recipe from a URL and a field schema."
          action={<RecipeCreateDialog onCreated={handleCreated} />}
        />
      ) : (
        <div className="rounded-lg border border-border">
          <RecipesTable recipes={recipes} />
        </div>
      )}
    </div>
  )
}
