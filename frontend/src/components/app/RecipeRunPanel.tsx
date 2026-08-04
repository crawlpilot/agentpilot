import { useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { Play, Wand2, Code } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Badge, type BadgeProps } from '@/components/ui/badge'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { CopyButton } from '@/components/app/CopyButton'
import { EmptyState } from '@/components/app/EmptyState'
import { useRunRecipe, useHealRecipe, useCodegenRecipe, useRecipeRun } from '@/hooks/useRecipes'
import { useRecentRuns } from '@/hooks/useRecentRuns'
import { useToast } from '@/components/ui/toast'
import type { RecipeCodegenLanguage, RecipeRunKind, RunStatus } from '@/lib/api/types'

const LANGUAGES: RecipeCodegenLanguage[] = ['python-playwright', 'node-puppeteer', 'python-requests-only']

function statusVariant(status: RunStatus): NonNullable<BadgeProps['variant']> {
  switch (status) {
    case 'completed':
      return 'success'
    case 'failed':
      return 'destructive'
    case 'running':
      return 'accent'
    case 'cancelled':
      return 'outline'
    default:
      return 'default'
  }
}

export function RecipeRunPanel({ recipeId, urlPattern }: { recipeId: string; urlPattern: string }) {
  const [searchParams, setSearchParams] = useSearchParams()
  const [language, setLanguage] = useState<RecipeCodegenLanguage>('python-playwright')

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

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-2">
        <Button size="sm" variant="outline" onClick={handleRun} disabled={runRecipe.isPending}>
          <Play className="size-3.5" />
          Run
        </Button>
        <Button size="sm" variant="outline" onClick={handleHeal} disabled={healRecipe.isPending}>
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
      </div>

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
              <pre className="max-h-[32rem] overflow-auto whitespace-pre-wrap p-3 text-xs">{generatedCode}</pre>
            </div>
          ) : run.status === 'completed' ? (
            <div className="flex flex-col gap-2">
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
