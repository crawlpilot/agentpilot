import { useNavigate, useParams } from 'react-router-dom'
import { ArrowLeft } from 'lucide-react'
import { useRecipe, useRecipeVersions } from '@/hooks/useRecipes'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Badge, type BadgeProps } from '@/components/ui/badge'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { RecipeFieldGroupsList } from '@/components/app/RecipeFieldGroupsList'
import { RecipeRunPanel } from '@/components/app/RecipeRunPanel'
import { EmptyState } from '@/components/app/EmptyState'
import { useAuth } from '@/lib/auth/AuthContext'
import type { RecipeHealthStatus } from '@/lib/api/types'

function healthVariant(status: RecipeHealthStatus): NonNullable<BadgeProps['variant']> {
  switch (status) {
    case 'healthy':
      return 'success'
    case 'degraded':
      return 'warning'
    case 'broken':
      return 'destructive'
  }
}

function DetailRow({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex justify-between gap-4">
      <span className="text-muted-foreground">{label}</span>
      <span className="text-right">{value}</span>
    </div>
  )
}

export function RecipeDetailPage() {
  const { recipeId } = useParams<{ recipeId: string }>()
  const navigate = useNavigate()
  const { isAuthed } = useAuth()
  const { data, isLoading } = useRecipe(recipeId ?? '')
  const { data: versionsData } = useRecipeVersions(recipeId ?? '')

  if (!recipeId) return null
  if (!isAuthed) {
    return <EmptyState title="Tenant API key required" description="Sign in with a tenant API key to view this recipe." />
  }
  if (isLoading) return null
  if (!data) {
    return (
      <EmptyState
        title="Recipe not found"
        action={
          <Button variant="outline" onClick={() => navigate('/recipes')}>
            Back to recipes
          </Button>
        }
      />
    )
  }

  const recipe = data.data
  const versions = versionsData?.versions ?? []

  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-center gap-2">
        <Button size="icon" variant="ghost" onClick={() => navigate('/recipes')}>
          <ArrowLeft className="size-4" />
        </Button>
        <h1 className="text-lg font-semibold">{recipe.name}</h1>
        <Badge variant={healthVariant(recipe.health_status)} className="capitalize">
          {recipe.health_status}
        </Badge>
      </div>

      <Card className="max-w-md">
        <CardHeader>
          <CardTitle>Details</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-2 text-sm">
          <DetailRow label="URL pattern" value={<span className="font-mono text-xs">{recipe.url_pattern}</span>} />
          <DetailRow label="Version" value={recipe.version} />
          <DetailRow
            label="Schedule"
            value={recipe.schedule_interval_seconds ? `every ${recipe.schedule_interval_seconds}s` : 'On-demand'}
          />
          <DetailRow
            label="Last verified"
            value={recipe.last_verified_at ? new Date(recipe.last_verified_at).toLocaleString() : '--'}
          />
          <DetailRow
            label="Last run"
            value={recipe.last_run_at ? new Date(recipe.last_run_at).toLocaleString() : '--'}
          />
        </CardContent>
      </Card>

      <div className="flex flex-col gap-2">
        <h2 className="text-sm font-medium uppercase tracking-wide text-muted-foreground">Actions</h2>
        <RecipeRunPanel recipeId={recipe.recipe_id} urlPattern={recipe.url_pattern} />
      </div>

      <div className="flex flex-col gap-2">
        <h2 className="text-sm font-medium uppercase tracking-wide text-muted-foreground">
          Fields ({recipe.field_groups.length})
        </h2>
        <RecipeFieldGroupsList groups={recipe.field_groups} />
      </div>

      <div className="flex flex-col gap-2">
        <h2 className="text-sm font-medium uppercase tracking-wide text-muted-foreground">Versions</h2>
        {versions.length === 0 ? (
          <EmptyState title="No versions yet" description="A version is recorded after each successful build or heal." />
        ) : (
          <div className="rounded-lg border border-border">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Version</TableHead>
                  <TableHead>Diff summary</TableHead>
                  <TableHead>Created</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {versions.map((v) => (
                  <TableRow key={v.version}>
                    <TableCell>{v.version}</TableCell>
                    <TableCell className="text-muted-foreground">{v.diff_summary ?? '--'}</TableCell>
                    <TableCell className="text-muted-foreground">{new Date(v.created_at).toLocaleString()}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        )}
      </div>
    </div>
  )
}
