import { CheckCircle2, Gavel, Loader2, RefreshCw, XCircle } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { RecipeOverview } from './RecipeOverview'
import type { RunProgress } from '@/lib/api/types'
import type { Recipe } from '@/lib/recipe/types'

/**
 * The check, while it is running.
 *
 * This phase decides whether the recipe is any good, and it used to be a single
 * "checking…" line followed by several minutes of nothing. That is the worst
 * place to go dark: the build has already done its work, the person is waiting
 * to find out whether it was any use, and every question they have — *did it
 * actually run? on which pages? what did it get? what does the reviewer make of
 * it?* — is being answered server-side and thrown away.
 *
 * So the recipe is shown **as it stands**, with the values from the last replay
 * against it, and the judge's findings land on the fields they are about. The
 * same `RecipeOverview` renders the finished article, so nothing has to be
 * re-read once the build ends — it is the same screen, filling in.
 */
export function CheckProgress({ progress }: { progress: RunProgress }) {
  const recipe = progress.recipe as Recipe | undefined
  const collected = progress.collected ?? {}
  const verdicts = progress.verdict?.fields ?? {}

  return (
    <div className="mx-auto flex w-full max-w-3xl flex-col items-center gap-4 p-6">
      <div className="flex flex-col items-center gap-2">
        <Stage progress={progress} />
        {progress.urls && progress.urls.length > 0 && (
          <p className="text-center text-xs text-muted-foreground">
            Running it back against {progress.urls.length} sample{' '}
            {progress.urls.length === 1 ? 'page' : 'pages'} — the first time the steps are
            tested as a sequence rather than one at a time.
          </p>
        )}
      </div>

      {progress.runs && progress.runs.length > 0 && <RunRow runs={progress.runs} />}

      {progress.rejected && Object.keys(progress.rejected).length > 0 && (
        <div className="w-full rounded-md border border-warning/40 bg-warning/10 p-3">
          <p className="text-xs font-medium">The reviewer pushed these back</p>
          <ul className="mt-1 flex flex-col gap-0.5">
            {Object.entries(progress.rejected).map(([name, reason]) => (
              <li key={name} className="text-[11px] text-muted-foreground">
                <code className="font-mono">{name}</code> — {reason}
              </li>
            ))}
          </ul>
          <p className="mt-1 text-[11px] text-muted-foreground">
            Its own words go back to the part that picks selectors, so the next attempt is told
            what it got wrong rather than just asked again.
          </p>
        </div>
      )}

      {recipe ? (
        <RecipeOverview recipe={recipe} values={collected} verdicts={verdicts} />
      ) : (
        <p className="text-sm text-muted-foreground">Assembling the recipe…</p>
      )}
    </div>
  )
}

function Stage({ progress }: { progress: RunProgress }) {
  const attempt = progress.attempt && progress.attempt > 1 ? ` · round ${progress.attempt}` : ''

  switch (progress.step) {
    case 'replayed':
      return (
        <Line icon={<CheckCircle2 className="size-4 text-success" />}>
          Ran the recipe{attempt}
        </Line>
      )
    case 'judging':
      return (
        <Line icon={<Gavel className="size-4 animate-pulse text-muted-foreground" />}>
          Checking the values against the page{attempt}
        </Line>
      )
    case 'judged':
      return progress.verdict?.errored ? (
        <Line icon={<XCircle className="size-4 text-warning" />}>
          The reviewer was unavailable — this draft is unchecked
        </Line>
      ) : progress.verdict?.passed ? (
        <Line icon={<CheckCircle2 className="size-4 text-success" />}>
          The values check out{attempt}
        </Line>
      ) : (
        <Line icon={<XCircle className="size-4 text-warning" />}>
          Some values look wrong{attempt}
        </Line>
      )
    case 'repairing':
      return (
        <Line icon={<RefreshCw className="size-4 animate-spin text-muted-foreground" />}>
          Re-binding what the reviewer rejected{attempt}
        </Line>
      )
    case 'repaired':
      return (
        <Line icon={<CheckCircle2 className="size-4 text-success" />}>
          Re-bound {progress.fields?.join(', ') || 'nothing'}{attempt}
        </Line>
      )
    default:
      return (
        <Line icon={<Loader2 className="size-4 animate-spin text-muted-foreground" />}>
          Running the recipe back against the page{attempt}
        </Line>
      )
  }
}

function Line({ icon, children }: { icon: React.ReactNode; children: React.ReactNode }) {
  return (
    <div className="flex items-center gap-2">
      {icon}
      <span className="text-sm font-medium">{children}</span>
    </div>
  )
}

function RunRow({ runs }: { runs: NonNullable<RunProgress['runs']> }) {
  return (
    <div className="flex w-full flex-col gap-1">
      {runs.map((run, i) => {
        const failed = (run.step_trace ?? []).filter((s) => s.status === 'failed').length
        const resolved = Object.values(run.field_status ?? {}).filter(
          (s) => s === 'resolved',
        ).length
        const total = Object.keys(run.field_status ?? {}).length
        return (
          <div key={i} className="flex flex-wrap items-center gap-2 text-[11px]">
            <Badge variant={run.outcome === 'ok' ? 'success' : 'warning'}>
              {run.outcome ?? 'unknown'}
            </Badge>
            {total > 0 && (
              <span className="text-muted-foreground">
                {resolved} of {total} fields
              </span>
            )}
            {failed > 0 && (
              <Badge variant="warning">
                {failed} step{failed === 1 ? '' : 's'} did not run
              </Badge>
            )}
            <code className="min-w-0 flex-1 truncate text-muted-foreground">{run.url}</code>
          </div>
        )
      })}
    </div>
  )
}
