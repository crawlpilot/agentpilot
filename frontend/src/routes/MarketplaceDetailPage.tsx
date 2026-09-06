import { useMemo, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { ArrowLeft, Loader2, PencilRuler, Play } from 'lucide-react'
import { useRecipe } from '@/hooks/useRecipes'
import { useRecipeJobs, useSubmitJob } from '@/hooks/useMarketplace'
import { EmptyState } from '@/components/app/EmptyState'
import { JobStatusBadge } from '@/components/app/JobStatusBadge'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Textarea } from '@/components/ui/textarea'
import { useToast } from '@/components/ui/toast'
import { useAuth } from '@/lib/auth/AuthContext'
import { parseUrls, rawPieceCount, suspiciousUrls } from '@/lib/recipe/submit'

/** What the backend accepts in one submission -- mirrors `RecipeJobRequest`. */
const MAX_URLS = 500

export function MarketplaceDetailPage() {
  const { recipeId = '' } = useParams<{ recipeId: string }>()
  const navigate = useNavigate()
  const { isAuthed } = useAuth()
  const { toast } = useToast()

  const { data, isLoading } = useRecipe(recipeId)
  const { data: jobsData } = useRecipeJobs(recipeId)
  const submit = useSubmitJob(recipeId)

  const [raw, setRaw] = useState('')
  const urls = useMemo(() => parseUrls(raw), [raw])
  const suspicious = useMemo(() => suspiciousUrls(urls), [urls])

  if (!isAuthed) {
    return (
      <EmptyState
        title="Tenant API key required"
        description="Sign in with a tenant API key to run a scraper."
      />
    )
  }
  if (isLoading) return null
  if (!data) {
    return (
      <EmptyState
        title="Scraper not found"
        description="It may have been unpublished, or it belongs to another tenant."
        action={
          <Button variant="outline" onClick={() => navigate('/marketplace')}>
            Back to the marketplace
          </Button>
        }
      />
    )
  }

  const recipe = data.data
  const fields = Object.keys(recipe.field_schema ?? {})
  const jobs = jobsData?.jobs ?? []
  const tooMany = urls.length > MAX_URLS

  function handleSubmit() {
    submit.mutate(
      { urls },
      {
        onSuccess: (resp) => navigate(`/marketplace/${recipeId}/jobs/${resp.job_id}`),
        onError: (err) =>
          toast({ title: 'Could not start the job', description: err.message, variant: 'destructive' }),
      },
    )
  }

  return (
    <div className="flex flex-col gap-4">
      <div>
        <Button variant="ghost" size="sm" asChild className="-ml-2">
          <Link to="/marketplace">
            <ArrowLeft className="size-3.5" />
            Marketplace
          </Link>
        </Button>
      </div>

      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <h1 className="text-xl font-semibold">{recipe.name}</h1>
          <p className="text-sm text-muted-foreground">
            {recipe.url_pattern || 'Runs on any URL you give it'} · v{recipe.version}
          </p>
        </div>
        <Button variant="outline" asChild>
          <Link to={`/recipes/${recipeId}`}>
            <PencilRuler className="size-4" />
            Recipe details
          </Link>
        </Button>
      </div>

      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_20rem]">
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Your URLs</CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col gap-3">
            <Textarea
              rows={10}
              value={raw}
              onChange={(e) => setRaw(e.target.value)}
              placeholder={'https://example.com/p/1\nhttps://example.com/p/2'}
              className="font-mono text-xs"
            />

            <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
              <span>
                {urls.length} URL{urls.length === 1 ? '' : 's'}
              </span>
              {/* Said only when it happened, so the count above is trusted
                  rather than second-guessed against what was pasted. */}
              {urls.length < rawPieceCount(raw) && (
                <span>· duplicates removed</span>
              )}
              {suspicious.length > 0 && (
                <span className="text-warning">
                  · {suspicious.length} without http(s), which will fail
                </span>
              )}
              {tooMany && (
                <span className="text-destructive">
                  · over the {MAX_URLS} limit for one job
                </span>
              )}
            </div>

            <div>
              <Button onClick={handleSubmit} disabled={urls.length === 0 || tooMany || submit.isPending}>
                {submit.isPending ? (
                  <Loader2 className="size-4 animate-spin" />
                ) : (
                  <Play className="size-4" />
                )}
                Run on {urls.length || 'your'} URL{urls.length === 1 ? '' : 's'}
              </Button>
            </div>
          </CardContent>
        </Card>

        <div className="flex flex-col gap-4">
          <Card>
            <CardHeader>
              <CardTitle className="text-base">What you get back</CardTitle>
            </CardHeader>
            <CardContent>
              {fields.length === 0 ? (
                <p className="text-sm text-muted-foreground">
                  This recipe declares no fields, so it would return nothing.
                </p>
              ) : (
                <div className="flex flex-wrap gap-1">
                  {fields.map((name) => (
                    <Badge key={name} variant="outline" className="font-mono text-[10px]">
                      {name}
                    </Badge>
                  ))}
                </div>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle className="text-base">Recent jobs</CardTitle>
            </CardHeader>
            <CardContent>
              {jobs.length === 0 ? (
                <p className="text-sm text-muted-foreground">You have not run this one yet.</p>
              ) : (
                <div className="flex flex-col gap-1">
                  {jobs.slice(0, 8).map((job) => (
                    <Link
                      key={job.job_id}
                      to={`/marketplace/${recipeId}/jobs/${job.job_id}`}
                      className="flex items-center justify-between gap-2 rounded-md px-2 py-1.5 text-xs hover:bg-muted"
                    >
                      <span className="text-muted-foreground">
                        {new Date(job.created_at).toLocaleString()}
                      </span>
                      <span className="flex items-center gap-1.5">
                        <span className="text-muted-foreground">
                          {job.completed}/{job.total}
                        </span>
                        <JobStatusBadge status={job.status} />
                      </span>
                    </Link>
                  ))}
                </div>
              )}
            </CardContent>
          </Card>
        </div>
      </div>
    </div>
  )
}
