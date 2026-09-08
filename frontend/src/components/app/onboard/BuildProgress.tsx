import { Check, Loader2, Search } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { LiveViewPanel } from '@/components/app/LiveViewPanel'
import { useSessionsList } from '@/hooks/useSessionsList'
import type { RunProgress } from '@/lib/api/types'

/**
 * A build in flight: the page it is driving, and what it is doing to it.
 *
 * A spinner was the wrong answer here. The build takes minutes, drives a real
 * browser through a real site, and can fail in ways that are obvious the
 * moment you see the page -- a cookie wall it did not dismiss, a consent
 * dialog, a login. Hiding all of that behind "Working on the page…" means the
 * one person who could recognise the problem in a second is the only one not
 * being shown it.
 *
 * The live view is the same screencast the sessions page uses, pointed at the
 * run's own session. Nothing new was needed for it: the run already opens
 * headful, and its session is named after the run.
 */
export function BuildProgress({ runId, progress }: { runId: string; progress: RunProgress }) {
  const { data } = useSessionsList()
  const sessions = (data?.sessions ?? []).filter((s) => s.state === 'active')
  const sessionId = sessions.find((s) => s.session_id === `recipe-run-${runId}`)?.session_id ?? null

  const steps = progress.steps ?? []
  const found = progress.found ?? []
  const remaining = progress.remaining ?? []
  const total = found.length + remaining.length

  return (
    <div className="flex h-[calc(100vh-6rem)] min-h-0 flex-col gap-3 p-4">
      <div className="flex flex-wrap items-center gap-2">
        <Loader2 className="size-4 animate-spin text-muted-foreground" />
        <span className="text-sm font-medium">
          {progress.phase === 'verifying'
            ? 'Checking what it collected'
            : 'Reading the page'}
        </span>
        {total > 0 && (
          <Badge variant="outline">
            {found.length} of {total} fields
          </Badge>
        )}
        <span className="text-xs text-muted-foreground">
          {progress.phase === 'verifying'
            ? 'Running the recipe back against your sample pages.'
            : 'Interacting with whatever it needs to reveal each value.'}
        </span>
      </div>

      <div className="grid min-h-0 flex-1 grid-cols-[minmax(0,1fr)_20rem] gap-3">
        <div className="min-h-0 overflow-hidden rounded-md border border-border">
          {sessionId ? (
            <LiveViewPanel sessionId={sessionId} />
          ) : (
            <div className="flex h-full items-center justify-center p-6 text-center text-sm text-muted-foreground">
              Waiting for the browser to open&hellip;
            </div>
          )}
        </div>

        <div className="flex min-h-0 flex-col gap-3 overflow-y-auto">
          {total > 0 && (
            <div className="flex flex-col gap-1">
              <span className="text-xs font-medium">Fields</span>
              <div className="flex flex-wrap gap-1">
                {found.map((name) => (
                  <Badge key={name} variant="success" className="gap-1">
                    <Check className="size-3" />
                    {name}
                  </Badge>
                ))}
                {remaining.map((name) => (
                  <Badge key={name} variant="outline" className="gap-1 opacity-70">
                    <Search className="size-3" />
                    {name}
                  </Badge>
                ))}
              </div>
            </div>
          )}

          <div className="flex flex-col gap-1">
            <span className="text-xs font-medium">What it did</span>
            {steps.length === 0 ? (
              <p className="text-xs text-muted-foreground">Starting up&hellip;</p>
            ) : (
              <ol className="flex flex-col gap-1.5">
                {[...steps].reverse().map((step) => (
                  <li
                    key={step.n}
                    className="rounded-md border border-border px-2 py-1.5 text-[11px]"
                  >
                    <div className="flex items-center gap-1.5">
                      <span className="text-muted-foreground">#{step.n}</span>
                      {step.actions.map((action, i) => (
                        <Badge key={`${action}-${i}`} variant="outline" className="px-1 py-0">
                          {action.replace(/Action$/, '').toLowerCase()}
                        </Badge>
                      ))}
                    </div>
                    {step.goal && <p className="mt-0.5 text-muted-foreground">{step.goal}</p>}
                    {step.found.length > 0 && (
                      <p className="mt-0.5 text-success">found {step.found.join(', ')}</p>
                    )}
                  </li>
                ))}
              </ol>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
