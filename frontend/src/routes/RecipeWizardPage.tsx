import { useCallback, useMemo, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { ArrowLeft, ArrowRight, SlidersHorizontal } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { EmptyState } from '@/components/app/EmptyState'
import { LintPanel } from '@/components/app/studio/LintPanel'
import { JsonTab } from '@/components/app/studio/JsonTab'
import { WizardSteps, type WizardStep } from '@/components/app/wizard/WizardSteps'
import { Step1Session } from '@/components/app/wizard/Step1Session'
import { Step2Pick } from '@/components/app/wizard/Step2Pick'
import { Step3Fields } from '@/components/app/wizard/Step3Fields'
import { Step4Pagination, type PaginationChoice } from '@/components/app/wizard/Step4Pagination'
import { usePagePicker } from '@/hooks/usePagePicker'
import { useRecipeDoc } from '@/hooks/useRecipeDoc'
import { useExecuteSession } from '@/hooks/useExecuteSession'
import { useToast } from '@/components/ui/toast'
import { emptyRecipe } from '@/lib/recipe/document'
import { lintRecipe } from '@/lib/recipe/lint'
import {
  applyDrafts,
  detailPickToDraft,
  listPickToDrafts,
  withJsonAlternatives,
  type FieldDraft,
} from '@/lib/recipe/fromPick'
import { PROBE_JS, findPaths, flattenProbe, hitToLocator, type PathHit, type ProbeResult } from '@/lib/recipe/probe'
import type { PickerMode } from '@/lib/picker/protocol'
import type { Recipe, Step } from '@/lib/recipe/types'

const STEPS: WizardStep[] = [
  { id: 'session', title: 'Page', hint: 'Which pages, and a browser to look at them in' },
  { id: 'pick', title: 'Pick', hint: 'Click what you want out of the page' },
  { id: 'fields', title: 'Fields', hint: 'Name what you picked and check its fallbacks' },
  { id: 'paging', title: 'More', hint: 'How to reach the rest of the results' },
  { id: 'review', title: 'Review', hint: 'What the recipe says, and what the lint makes of it' },
]

/**
 * The guided path to a v2 recipe.
 *
 * This is the front door; `RecipeStudioPage` is still there behind it for
 * everything this does not cover -- steps, variants, transform pipelines,
 * per-candidate predicates. The split is the point. The three-pane editor is a
 * good editor and a bad on-ramp: it asks an author to hand-type CSS selectors
 * into a document with seven locator kinds and twenty-seven step ops before
 * they have seen anything work. Here, clicking the page produces a valid
 * document, and the editor is where you go when you need more than that.
 *
 * Both write through `useRecipeDoc` under the *same* draft key, so "Open in
 * advanced editor" is a navigation, not an export.
 */
export function RecipeWizardPage() {
  const { recipeId } = useParams<{ recipeId?: string }>()
  const draftKey = recipeId ?? 'new'
  const navigate = useNavigate()
  const { toast } = useToast()

  const seed = useMemo<Recipe>(() => emptyRecipe(''), [])
  const { doc, update, reset } = useRecipeDoc(draftKey, seed)

  const [step, setStep] = useState(0)
  const [furthest, setFurthest] = useState(0)
  const [sessionId, setSessionId] = useState<string | null>(null)
  const [pickMode, setPickMode] = useState<Exclude<PickerMode, 'single'>>('list')
  const [drafts, setDrafts] = useState<FieldDraft[]>([])
  const [paging, setPaging] = useState<PaginationChoice>({ mode: 'none', maxPages: 5 })
  const [hits, setHits] = useState<PathHit[] | null>(null)

  const picker = usePagePicker(sessionId)
  const execute = useExecuteSession()

  function goTo(next: number) {
    setStep(next)
    setFurthest((f) => Math.max(f, next))
  }

  /**
   * Read the page's embedded JSON once, in the background, when picking starts.
   *
   * This is what makes the "find in JSON" offer possible at all, and doing it
   * eagerly matters: the author picked a rendered element because that is what
   * they could see, and the moment to tell them the same value has a durable
   * path is while they are still looking at the field, not after they have
   * moved on.
   */
  const probeJson = useCallback(() => {
    if (!sessionId || hits) return
    execute.mutate(
      { sessionId, actions: [{ type: 'execute_js', script: PROBE_JS }] },
      {
        onSuccess: (result) => {
          const raw = result.js_returns[0] as ProbeResult | null
          if (!raw || typeof raw !== 'object') return
          setHits(
            flattenProbe({ json_ld: raw.json_ld ?? [], meta: raw.meta ?? {}, hydration: raw.hydration ?? {} }),
          )
        },
        // A page with no embedded JSON is a normal outcome, not an error --
        // it just means every candidate here will be a DOM one.
        onError: () => setHits([]),
      },
    )
  }, [execute, hits, sessionId])

  async function startPick() {
    probeJson()
    try {
      const payload = await picker.pick(pickMode)
      if (!payload) return

      if (payload.selectionMode === 'list') {
        const { drafts: picked, count } = listPickToDrafts(payload)
        if (picked.length === 0) {
          toast({ title: 'Nothing readable in that item', description: 'Try a wider selection.' })
          return
        }
        setDrafts(picked)
        toast({
          title: `${picked.length} field${picked.length === 1 ? '' : 's'} from ${count} rows`,
          description: 'Name them on the next step.',
        })
      } else {
        const draft = detailPickToDraft(payload, drafts.map((d) => d.name))
        setDrafts((current) => [...current, draft])
      }
      goTo(2)
    } catch {
      // usePagePicker surfaces the message in `picker.error`.
    }
  }

  async function pickNextButton() {
    const payload = await picker.pick('single')
    if (!payload) return
    const selector = payload.itemSelectors?.[0]?.selector ?? payload.containerSelector
    setPaging((p) => ({ ...p, selector }))
  }

  /** Offer a structured-data path above a picked CSS candidate. */
  function findInJson(draft: FieldDraft, index: number) {
    if (!draft.preview || !hits?.length) return
    const matches = findPaths(hits, draft.preview).slice(0, 2)
    if (matches.length === 0) {
      toast({
        title: 'Not in the page’s JSON',
        description: 'That is a real answer — this one has to come from the DOM.',
      })
      return
    }
    setDrafts((current) =>
      current.map((d, i) =>
        i === index ? { ...d, candidates: withJsonAlternatives(d.candidates, matches.map((hit) => hitToLocator(hit))) } : d,
      ),
    )
    toast({
      title: `Found in ${matches[0].kind}`,
      description: 'Added above the picked selector — it will be tried first.',
    })
  }

  /** Fold the wizard's state into the document. */
  const built = useMemo(() => {
    let recipe = applyDrafts(doc, drafts, { groupId: 'core' })
    const setup: Step[] = []
    if (paging.mode === 'next' && paging.selector) {
      setup.push({
        op: 'click',
        target: { kind: 'css', selector: paging.selector },
        // Pagination is best-effort by nature: the last page has no next
        // button, and that must not fail the run.
        on_error: 'continue',
        optional: true,
      })
    } else if (paging.mode === 'scroll') {
      setup.push({ op: 'scroll', args: { to: 'bottom' }, on_error: 'continue', optional: true })
    }
    if (setup.length > 0) recipe = { ...recipe, global_setup: [...(recipe.global_setup ?? []), ...setup] }
    return recipe
  }, [doc, drafts, paging])

  const issues = useMemo(() => lintRecipe(built), [built])
  const errors = issues.filter((i) => i.severity === 'error').length

  function commitAndOpenEditor() {
    reset(built)
    navigate(recipeId ? `/recipes/${recipeId}/studio` : '/recipes/new/studio')
  }

  const canAdvance =
    (step === 0 && !!sessionId && doc.name.trim().length > 0) ||
    (step === 1 && drafts.length > 0) ||
    (step === 2 && drafts.length > 0) ||
    step === 3

  return (
    <div className="flex h-screen flex-col bg-background">
      <header className="flex shrink-0 flex-wrap items-center gap-2 border-b border-border px-3 py-2">
        <Button variant="ghost" size="sm" asChild>
          <Link to="/recipes">
            <ArrowLeft className="size-3.5" />
            Recipes
          </Link>
        </Button>
        <span className="text-sm font-medium">{doc.name || 'New recipe'}</span>
        {drafts.length > 0 && <Badge variant="outline">{drafts.length} fields</Badge>}
        {errors > 0 && <Badge variant="destructive">{errors} errors</Badge>}
        <div className="ml-auto flex items-center gap-2">
          <Button variant="outline" size="sm" onClick={commitAndOpenEditor}>
            <SlidersHorizontal className="size-3.5" />
            Advanced editor
          </Button>
        </div>
      </header>

      <WizardSteps steps={STEPS} current={step} furthest={furthest} onGoTo={setStep} />

      <main className="min-h-0 flex-1 overflow-y-auto border-t border-border">
        {step === 0 && (
          <Step1Session
            sessionId={sessionId}
            onSessionChange={setSessionId}
            sampleUrls={doc.sample_urls ?? []}
            onSampleUrlsChange={(urls) => update((r) => ({ ...r, sample_urls: urls }))}
            name={doc.name}
            onNameChange={(name) => update((r) => ({ ...r, name }))}
          />
        )}

        {step === 1 &&
          (sessionId ? (
            <Step2Pick
              sessionId={sessionId}
              mode={pickMode}
              onModeChange={setPickMode}
              status={picker.status}
              fieldCount={drafts.length}
              onStart={() => void startPick()}
              onCancel={picker.cancel}
              onRefine={picker.refine}
            />
          ) : (
            <div className="flex h-full items-center justify-center p-6">
              <EmptyState title="No session" description="Go back and open one." />
            </div>
          ))}

        {step === 2 && (
          <Step3Fields
            drafts={drafts}
            onChange={setDrafts}
            onTestSelector={sessionId ? picker.testSelector : undefined}
            onFindInJson={findInJson}
            jsonProbeReady={(hits?.length ?? 0) > 0}
          />
        )}

        {step === 3 && (
          <Step4Pagination
            value={paging}
            onChange={setPaging}
            status={picker.status}
            onPickButton={() => void pickNextButton()}
            onCancel={picker.cancel}
            onRefine={picker.refine}
          />
        )}

        {step === 4 && (
          <div className="mx-auto flex w-full max-w-3xl flex-col gap-3 p-4">
            <div className="rounded-md border border-border">
              <LintPanel issues={issues} onJump={() => setStep(2)} />
            </div>
            <JsonTab recipe={built} onReplace={(next) => reset(next)} />
          </div>
        )}
      </main>

      <footer className="flex shrink-0 items-center gap-2 border-t border-border px-3 py-2">
        {picker.error && <span className="text-xs text-destructive">{picker.error}</span>}
        <div className="ml-auto flex items-center gap-2">
          {step > 0 && (
            <Button variant="outline" size="sm" onClick={() => setStep(step - 1)}>
              Back
            </Button>
          )}
          {step < STEPS.length - 1 && (
            <Button size="sm" disabled={!canAdvance} onClick={() => goTo(step + 1)}>
              Next
              <ArrowRight className="size-3.5" />
            </Button>
          )}
          {step === STEPS.length - 1 && (
            <Button size="sm" onClick={commitAndOpenEditor}>
              Open in advanced editor
              <ArrowRight className="size-3.5" />
            </Button>
          )}
        </div>
      </footer>
    </div>
  )
}
