import { CheckCircle2, Hand, Loader2, XCircle } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { useRecipeRun, useRequestAssist } from '@/hooks/useRecipes'
import { AssistPanel } from './AssistPanel'
import { BuildProgress } from './BuildProgress'
import { CheckProgress } from './CheckProgress'
import { RecipeOverview } from './RecipeOverview'
import type { PendingAsk, RunProgress } from '@/lib/api/types'
import type { Recipe } from '@/lib/recipe/types'
import { useRecipe } from '@/hooks/useRecipes'
import { Link } from 'react-router-dom'

/**
 * What the build is doing, and the one place it can stop and ask.
 *
 * A parked run is not a failure and not progress -- it is waiting on the person
 * looking at this screen, and the whole point of the mechanism is that its
 * browser session is still open on the page it got stuck on. So the ask is
 * shown here, inline, rather than as a notification to come back to later: the
 * session is a wasting asset.
 */
export function OnboardProgress({
  recipeId,
  runId,
  onDone,
}: {
  recipeId: string
  runId: string
  onDone: () => void
}) {
  const { data } = useRecipeRun(recipeId, runId)
  const run = data?.data
  // The built document, so the completed screen can show the scraper rather
  // than a status. Polls alongside the run, so it is present the moment the
  // worker writes it.
  const recipe = useRecipe(recipeId).data?.data?.document as Recipe | undefined

  if (!run) {
    return (
      <Centered>
        <Loader2 className="size-5 animate-spin text-muted-foreground" />
        <p className="text-sm text-muted-foreground">Queued…</p>
      </Centered>
    )
  }

  if (run.status === 'needs_input') {
    return (
      <AssistPanel
        recipeId={recipeId}
        runId={runId}
        asks={(run.pending_asks ?? []) as PendingAsk[]}
      />
    )
  }

  if (run.status === 'failed') {
    return (
      <Centered>
        <XCircle className="size-6 text-destructive" />
        <p className="text-sm font-medium">The build did not finish</p>
        <p className="max-w-md text-center text-sm text-muted-foreground">
          {run.error ?? 'It failed without saying why.'}
        </p>
      </Centered>
    )
  }

  if (run.status === 'completed') {
    const failures = (run.field_failures ?? {}) as Record<string, unknown>
    const unresolved = Object.keys((failures.unresolved as object) ?? {})
    const rejected = Object.keys((failures.rejected as object) ?? {})
    const payload = (run.data ?? {}) as Record<string, unknown>
    const ready = Boolean(payload.ready_for_review)
    const review = (payload.review ?? {}) as {
      runs?: Array<{
        url?: string
        outcome?: string
        data?: Record<string, unknown>
        step_trace?: Array<Record<string, unknown>>
      }>
      verdict?: {
        errored?: boolean
        fields?: Record<string, { ok: boolean; reason?: string }>
      } | null
      repairs?: number
    }
    // The first sample run's data is what a job against this recipe would
    // return, which is the only reading that predicts anything.
    const collected = review.runs?.[0]?.data ?? {}

    return (
      <div className="mx-auto flex w-full max-w-3xl flex-col items-center gap-4 p-6">
        <div className="flex flex-col items-center gap-2">
          <CheckCircle2 className="size-6 text-success" />
          <p className="text-sm font-medium">Your scraper is ready</p>
          <div className="flex flex-wrap items-center justify-center gap-2">
            {ready ? (
              <Badge variant="success">checked against the page</Badge>
            ) : (
              <Badge variant="warning">needs a look</Badge>
            )}
            {unresolved.length > 0 && (
              <Badge variant="outline">{unresolved.length} not found</Badge>
            )}
            {rejected.length > 0 && (
              <Badge variant="warning">{rejected.length} looked wrong</Badge>
            )}
            {(review.repairs ?? 0) > 0 && (
              <Badge variant="outline">{review.repairs} re-bound</Badge>
            )}
          </div>
        </div>

        {review.verdict?.errored && (
          <p className="w-full rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-xs text-muted-foreground">
            The reviewer that normally checks these values against the page was unavailable, so
            this draft is unchecked. Worth reading carefully.
          </p>
        )}

        {recipe ? (
          <RecipeOverview
            recipe={recipe}
            values={collected}
            verdicts={review.verdict?.fields ?? {}}
          />
        ) : (
          <p className="text-sm text-muted-foreground">Loading the recipe…</p>
        )}

        <RunEvidence runs={review.runs ?? []} />

        <div className="flex flex-wrap items-center justify-center gap-2 pt-2">
          <Button onClick={onDone}>Edit it</Button>
          <Button variant="outline" asChild>
            <Link to={`/marketplace/${recipeId}`}>Run it on your URLs</Link>
          </Button>
          <Button variant="ghost" asChild>
            <Link to={`/recipes/${recipeId}`}>Recipe details</Link>
          </Button>
        </div>
      </div>
    )
  }

  // Running. Show the browser it is driving and what it is doing to it -- the
  // build takes minutes, and a spinner hides exactly the evidence (a cookie
  // wall, a consent dialog, a login) that a person could act on in a second.
  //
  // Deliberately NOT gated on `progress` being present. The first write only
  // lands after the first exploration step completes, which is a browser
  // launch, a page load and two LLM round-trips later -- the better part of a
  // minute during which the browser is visibly doing the most interesting
  // thing it will do all run. Gating on progress hid the live view for exactly
  // that window and told the person nobody had picked their build up yet,
  // which was not true and was the one thing they could see was not true.
  //
  // `BuildProgress` already renders an empty payload correctly: "Starting up…"
  // for the steps, and its own placeholder until the session exists.
  if (run.status === 'running') {
    const progress = (run.progress ?? {}) as RunProgress
    // Two different screens for two different questions. While it explores,
    // what matters is the page it is driving. Once it is checking, the page is
    // no longer the interesting thing -- the recipe and what the reviewer makes
    // of it are.
    return (
      <div className="flex h-full min-h-0 flex-col">
        <RequestAssistBar recipeId={recipeId} runId={runId} />
        <div className="min-h-0 flex-1">
          {progress.phase === 'verifying' ? (
            <CheckProgress progress={progress} />
          ) : (
            <BuildProgress runId={runId} progress={progress} />
          )}
        </div>
      </div>
    )
  }

  return (
    <Centered>
      <Loader2 className="size-5 animate-spin text-muted-foreground" />
      <p className="text-sm font-medium">Queued</p>
      <p className="max-w-md text-center text-sm text-muted-foreground">
        Waiting for a worker to pick this up. The page will appear here once one does.
      </p>
    </Centered>
  )
}

/**
 * What actually happened when the recipe was run back against real pages.
 *
 * The verdict badges above say whether it worked; this says why. A step that
 * matched nothing is otherwise completely silent -- every reveal step is
 * `optional`/`on_error: continue` by construction -- so a field that came back
 * empty because its click never landed looks identical to one whose selector
 * is wrong, and the two need opposite fixes.
 */
function RunEvidence({
  runs,
}: {
  runs: Array<{ url?: string; outcome?: string; step_trace?: Array<Record<string, unknown>> }>
}) {
  if (runs.length === 0) return null

  return (
    <details className="w-full max-w-3xl rounded-md border border-border">
      <summary className="cursor-pointer px-3 py-2 text-xs font-medium">
        What it did on {runs.length} sample {runs.length === 1 ? 'page' : 'pages'}
      </summary>
      <div className="flex flex-col gap-3 border-t border-border p-3">
        {runs.map((run, i) => {
          const trace = run.step_trace ?? []
          const failed = trace.filter((s) => s.status === 'failed').length
          return (
            <div key={i} className="flex flex-col gap-1">
              <div className="flex flex-wrap items-center gap-2">
                <Badge variant={run.outcome === 'ok' ? 'success' : 'destructive'}>
                  {run.outcome ?? 'unknown'}
                </Badge>
                <code className="truncate text-[11px] text-muted-foreground">{run.url}</code>
                {failed > 0 && (
                  <Badge variant="warning">
                    {failed} step{failed === 1 ? '' : 's'} did not run
                  </Badge>
                )}
              </div>
              {trace.length > 0 && (
                <div className="flex flex-wrap gap-1">
                  {trace.map((step, j) => (
                    <Badge
                      key={j}
                      variant={step.status === 'failed' ? 'warning' : 'outline'}
                      className="font-normal"
                      title={String(step.reason ?? '')}
                    >
                      {String(step.op ?? '')}
                      {step.status === 'skipped' ? ' (skipped)' : ''}
                    </Badge>
                  ))}
                </div>
              )}
            </div>
          )
        })}
      </div>
    </details>
  )
}

/**
 * "Stop when you get to the end and let me look."
 *
 * `requestAssist` and `useRequestAssist` have been wired since the assist loop
 * shipped and were called by nothing -- so the only way a build ever stopped
 * for a person was for it to fail to settle a field by itself. This is the
 * other half: watching it bind something that looks wrong and being able to say
 * so while the session is still open.
 *
 * It sets a flag the worker re-reads *while the build is running*
 * (`_assist_wanted`), so it parks at the end of this build rather than
 * interrupting mid-step and losing the page state the ask depends on.
 */
function RequestAssistBar({ recipeId, runId }: { recipeId: string; runId: string }) {
  const request = useRequestAssist(recipeId, runId)
  const asked = request.isSuccess

  return (
    <div className="flex items-center gap-2 border-b border-border px-4 py-2">
      <span className="text-xs text-muted-foreground">
        {asked
          ? 'It will stop and show you every field before saving.'
          : 'It only stops for fields it cannot settle.'}
      </span>
      <Button
        size="sm"
        variant="outline"
        className="ml-auto h-7 text-xs"
        disabled={asked || request.isPending}
        onClick={() => request.mutate()}
      >
        <Hand className="size-3.5" />
        {asked ? 'Will stop for you' : request.isPending ? 'Asking…' : 'Stop and let me check'}
      </Button>
    </div>
  )
}

function Centered({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex min-h-[60vh] flex-col items-center justify-center gap-3 p-6">
      {children}
    </div>
  )
}
