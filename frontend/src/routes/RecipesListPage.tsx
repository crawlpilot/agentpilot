import { Link } from 'react-router-dom'
import { BookOpen, PencilRuler, Sparkles } from 'lucide-react'
import { useRecipesList } from '@/hooks/useRecipes'
import { RecipesTable } from '@/components/app/RecipesTable'
import { EmptyState } from '@/components/app/EmptyState'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { useAuth } from '@/lib/auth/AuthContext'

/**
 * The one front door.
 *
 * This used to sit beside `RecipeCreateDialog` as a second, competing "create"
 * button -- one opening the three-pane editor, the other a form whose field
 * schema is a raw-JSON textarea. Two entry points to the same object, neither
 * obviously the right one, is a choice the author should not have to make.
 * The wizard is the answer for a new recipe; the editor is reachable from
 * inside it, and from every recipe's detail page.
 */
function NewRecipeLink() {
  return (
    <div className="flex items-center gap-2">
      <Button asChild>
        <Link to="/recipes/onboard">
          <Sparkles className="size-4" />
          Build with AI
        </Link>
      </Button>
      <Button asChild variant="outline">
        <Link to="/recipes/new">
          <PencilRuler className="size-4" />
          Build by hand
        </Link>
      </Button>
    </div>
  )
}

export function RecipesListPage() {
  const { isAuthed } = useAuth()
  const { data, isLoading, isError } = useRecipesList()
  const recipes = data?.recipes ?? []

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold">Recipes</h1>
          <p className="text-sm text-muted-foreground">
            Reusable, self-healing extraction recipes -- built once, replayed on demand or on a schedule.
          </p>
        </div>
        {isAuthed && (
          <NewRecipeLink />
        )}
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
          description="Point at a page, click what you want out of it, and the recipe is written for you."
          action={<NewRecipeLink />}
        />
      ) : (
        <div className="rounded-lg border border-border">
          <RecipesTable recipes={recipes} />
        </div>
      )}
    </div>
  )
}
