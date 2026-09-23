import { useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { Play, Wand2, Code, FileJson } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Badge, type BadgeProps } from '@/components/ui/badge'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { CopyButton } from '@/components/app/CopyButton'
import { EmptyState } from '@/components/app/EmptyState'
import { RunFieldDetails } from '@/components/app/RunFieldDetails'
import { BuildTracePanel } from '@/components/app/BuildTracePanel'
import { useRunRecipe, useHealRecipe, useCodegenRecipe, useRecipeRun } from '@/hooks/useRecipes'
import { useRecentRuns } from '@/hooks/useRecentRuns'
import { useToast } from '@/components/ui/toast'
import type { RecipeCodegenLanguage, RecipeRunKind, RecipeRunStatus } from '@/lib/api/types'
import type { AssertionResult, FieldProvenance, FieldStatus } from '@/lib/recipe/types'

// Only what the v2 generator can actually emit. `node-puppeteer` and
// `python-requests-only` exist in the v1 module and are each one language pack
// away, but offering them here would offer a run that can only fail: every
// recipe the studio or the onboarding agent produces is v2.
const LANGUAGES: RecipeCodegenLanguage[] = ['python-playwright']

function statusVariant(status: RecipeRunStatus): NonNullable<BadgeProps['variant']> {
  switch (status) {
    case 'completed':
      return 'success'
    case 'failed':
      return 'destructive'
    case 'running':
      return 'accent'
    case 'needs_input':
      // Neither a failure nor progress: it is waiting on the person
      // reading this, which should look like neither.
      return 'warning'
    case 'cancelled':
      return 'outline'
    default:
      return 'default'
  }
}

export function RecipeRunPanel({
  recipeId,
  urlPattern,
  document: recipeDocument,
}: {
  recipeId: string
  urlPattern: string
  /**
   * The v2 document — what replay actually executes, and what the marketplace
   * publishes as a template. Passed down rather than fetched again here: the
   * detail page already has it.
   */
  document?: Record<string, unknown> | null
}) {
  const [searchParams, setSearchParams] = useSearchParams()
  const [language, setLanguage] = useState<RecipeCodegenLanguage>('python-playwright')
  const [showRaw, setShowRaw] = useState(false)

  const activeRunId = searchParams.get('runId')

  const runRecipe = useRunRecipe(recipeId)
  const healRecipe = useHealRecipe(recipeId)
  const codegenRecipe = useCodegenRecipe(recipeId)
  const { data: runData } = useRecipeRun(recipeId, activeRunId)
  const { append } = useRecentRuns('recipe')
  const { toast } = useToast()

  function focusRun(runId: string, kind: RecipeRunKind) {
    setSearchParams({ runId, kind })
    append({ endpoint: 'recipe', url: urlPattern, status: 'queued', jobId: runId, recipeId, kind })
  }

  function handleRun() {
    runRecipe.mutate(undefined, {
      onSuccess: (resp) => focusRun(resp.run_id, 'replay'),
      onError: (err) => toast({ title: 'Run failed', description: err.message, variant: 'destructive' }),
    })
  }

  function handleHeal() {
    healRecipe.mutate(undefined, {
      onSuccess: (resp) => focusRun(resp.run_id, 'heal'),
      onError: (err) => toast({ title: 'Heal failed', description: err.message, variant: 'destructive' }),
    })
  }

  function handleCodegen() {
    codegenRecipe.mutate(language, {
      onSuccess: (resp) => focusRun(resp.run_id, 'codegen'),
      onError: (err) => toast({ title: 'Codegen failed', description: err.message, variant: 'destructive' }),
    })
  }

  const run = runData?.data
  const generatedCode = run?.kind === 'codegen' && run.data ? (run.data.code as string | undefined) : undefined
  // What the generated script failed to check out on. Shown WITH the code
  // rather than instead of it: a script missing one field is still worth
  // having, as long as what is wrong with it is said out loud rather than
  // discovered in production.
  const codegenProblems =
    run?.kind === 'codegen' && run.data ? ((run.data.problems as string[] | undefined) ?? []) : []

  // A recipe with no target matcher has no URL of its own, which is the normal
  // shape for one authored in the studio: it is applied to URLs a caller
  // submits. `Run` would queue a replay with nowhere to navigate, so it points
  // at the place that takes URLs instead of failing in the worker.
  const hasOwnUrl = urlPattern.trim().length > 0

  // Serialized once per render rather than inside the collapsed branch, so the
  // copy button has something to copy the moment the panel opens.
  const hasDocument = Boolean(recipeDocument && Object.keys(recipeDocument).length > 0)
  const rawDocument = hasDocument ? JSON.stringify(recipeDocument, null, 2) : ''

  // The worker puts everything about *how* a value was collected into
  // `field_failures` -- which is a misleading name for it, but it is the column
  // that exists and the shape a caller reading the API already gets.
  const failures = (run?.field_failures ?? {}) as Record<string, unknown>
  const provenance = failures.provenance as Record<string, FieldProvenance> | undefined
  const fieldStatus = (failures.field_status ?? {}) as Record<string, FieldStatus>
  const runAssertions = (failures.assertions ?? {}) as Record<string, AssertionResult[]>
  const runTruncated = (failures.truncated ?? {}) as Record<string, boolean>

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-2">
        {hasOwnUrl && (
          <Button size="sm" variant="outline" onClick={handleRun} disabled={runRecipe.isPending}>
            <Play className="size-3.5" />
            Run
          </Button>
        )}
        {/*
          Offered whether or not the recipe has a URL of its own. It used to
          appear only when it had none, which is backwards: a recipe built
          against a real page is the one most worth applying to other pages of
          the same kind, and it was the only one that never offered the door.
        */}
        <Button size="sm" variant={hasOwnUrl ? 'outline' : 'default'} asChild>
          <Link to={`/marketplace/${recipeId}`}>
            <Play className="size-3.5" />
            Run on your URLs
          </Link>
        </Button>
        <Button
          size="sm"
          variant="outline"
          onClick={handleHeal}
          disabled={healRecipe.isPending || !hasOwnUrl}
          title={hasOwnUrl ? undefined : 'Healing re-runs the recipe against its own URL, and this one has none.'}
        >
          <Wand2 className="size-3.5" />
          Heal
        </Button>
        <div className="flex items-center gap-1.5">
          <Select value={language} onValueChange={(v) => setLanguage(v as RecipeCodegenLanguage)}>
            <SelectTrigger className="w-52">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {LANGUAGES.map((lang) => (
                <SelectItem key={lang} value={lang}>
                  {lang}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Button size="sm" variant="outline" onClick={handleCodegen} disabled={codegenRecipe.isPending}>
            <Code className="size-3.5" />
            Generate
          </Button>
        </div>
        {/*
          The document replay actually executes, next to the button that runs
          it. A run that collects the wrong value is a question about the
          recipe -- which locator won, what its `within` was scoped to, which
          steps the group carries -- and until this was here the only way to
          read that was the studio or a curl against the API.
        */}
        <Button
          size="sm"
          variant="outline"
          onClick={() => setShowRaw((open) => !open)}
          disabled={!hasDocument}
          aria-expanded={showRaw}
          title={
            hasDocument
              ? undefined
              : 'This recipe has no v2 document yet — it has not finished building.'
          }
        >
          <FileJson className="size-3.5" />
          {showRaw ? 'Hide raw recipe' : 'Raw recipe'}
        </Button>
      </div>

      {showRaw && hasDocument && (
        <div className="rounded-md border border-border">
          <div className="flex items-center justify-between gap-2 border-b border-border px-2 py-1">
            <span className="text-xs text-muted-foreground">
              The v2 document &mdash; what replay executes, and what the marketplace
              publishes as a template.
            </span>
            <CopyButton text={rawDocument} />
          </div>
          <pre className="max-h-[32rem] overflow-auto whitespace-pre-wrap p-3 text-xs">
            {rawDocument}
          </pre>
        </div>
      )}

      {!hasOwnUrl && (
        <p className="text-xs text-muted-foreground">
          This recipe declares no target URL, so it has nothing of its own to run against &mdash;
          submit your own URLs to it instead. Give it a target matcher if you want it to run on a
          schedule.
        </p>
      )}

      {activeRunId && run && (
        <div className="flex flex-col gap-2 rounded-md border border-border p-3">
          <div className="flex items-center gap-2">
            <Badge variant="outline" className="capitalize">
              {run.kind}
            </Badge>
            <Badge variant={statusVariant(run.status)} className="capitalize">
              {run.status}
            </Badge>
          </div>

          {/*
            Shown for any focused run, including a failed one -- that is the run
            whose trace matters most, and it is exactly the case where `data` is
            empty and there is otherwise nothing on screen to look at.
          */}
          {activeRunId && <BuildTracePanel recipeId={recipeId} runId={activeRunId} />}

          {run.status === 'failed' && (
            <div className="flex items-start gap-2 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm">
              <Badge variant="destructive">failed</Badge>
              <span className="text-muted-foreground">{run.error ?? 'the run failed without an error message'}</span>
            </div>
          )}

          {generatedCode !== undefined ? (
            <div className="rounded-md border border-border">
              <div className="flex items-center justify-between border-b border-border px-2 py-1">
                <Badge variant="outline">{(run.data?.language as string) ?? language}</Badge>
                <CopyButton text={generatedCode} />
              </div>
              {codegenProblems.length > 0 && (
                <div className="border-b border-warning/40 bg-warning/10 px-3 py-2">
                  <p className="text-xs font-medium">This script did not fully check out</p>
                  <ul className="mt-1 list-disc pl-4 text-[11px] text-muted-foreground">
                    {codegenProblems.map((problem) => (
                      <li key={problem}>{problem}</li>
                    ))}
                  </ul>
                </div>
              )}
              <pre className="max-h-[32rem] overflow-auto whitespace-pre-wrap p-3 text-xs">{generatedCode}</pre>
            </div>
          ) : run.status === 'completed' ? (
            <div className="flex flex-col gap-2">
              {/*
                Which selector produced each value, before the values
                themselves. A recipe binds a CHAIN per field, and the raw data
                dump below shows only what came out the end of it -- so a value
                that looks wrong used to raise a question nothing on this page
                could answer.
              */}
              {provenance && (
                <div className="flex flex-col gap-1">
                  <span className="text-xs text-muted-foreground">Selectors that matched</span>
                  <RunFieldDetails
                    data={(run.data ?? {}) as Record<string, unknown>}
                    provenance={provenance}
                    fieldStatus={fieldStatus}
                    assertions={runAssertions}
                    truncated={runTruncated}
                  />
                </div>
              )}
              <pre className="max-h-96 overflow-auto whitespace-pre-wrap rounded-md border border-border p-3 text-xs">
                {JSON.stringify(run.data ?? {}, null, 2)}
              </pre>
              {run.field_failures && Object.keys(run.field_failures).length > 0 && (
                <div>
                  <span className="text-xs text-muted-foreground">Field failures</span>
                  <pre className="mt-1 max-h-64 overflow-auto whitespace-pre-wrap rounded-md border border-destructive/40 p-2 text-xs">
                    {JSON.stringify(run.field_failures, null, 2)}
                  </pre>
                </div>
              )}
            </div>
          ) : run.status !== 'failed' ? (
            <EmptyState title="Running…" description="Waiting for the worker to finish this run." />
          ) : null}
        </div>
      )}
    </div>
  )
}
