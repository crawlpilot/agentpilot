import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { ArrowLeft, ArrowRight, SlidersHorizontal } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { EmptyState } from '@/components/app/EmptyState'
import { LiveViewPanel } from '@/components/app/LiveViewPanel'
import { LintPanel } from '@/components/app/studio/LintPanel'
import { JsonTab } from '@/components/app/studio/JsonTab'
import { WizardSteps, type WizardStep } from '@/components/app/wizard/WizardSteps'
import { Step1Session } from '@/components/app/wizard/Step1Session'
import { StepExtract } from '@/components/app/wizard/StepExtract'
import { Step4Pagination, type PaginationChoice } from '@/components/app/wizard/Step4Pagination'
import { StepPreview } from '@/components/app/wizard/StepPreview'
import { usePagePicker } from '@/hooks/usePagePicker'
import { useRecipeDoc } from '@/hooks/useRecipeDoc'
import { useExecuteSession } from '@/hooks/useExecuteSession'
import { useToast } from '@/components/ui/toast'
import { emptyRecipe } from '@/lib/recipe/document'
import { lintRecipe } from '@/lib/recipe/lint'
import {
  detailPickToDraft,
  itemsToRecipe,
  listPickToDrafts,
  stepsToHighlightFields,
  toHighlightFields,
  toPreviewFields,
  withJsonAlternatives,
  type FieldDraft,
  type WorkItem,
} from '@/lib/recipe/fromPick'
import { PROBE_JS, findPaths, flattenProbe, hitToLocator, type PathHit, type ProbeResult } from '@/lib/recipe/probe'
import type { PickerMode } from '@/lib/picker/protocol'
import type { PreviewResult } from '@/lib/picker/preview'
import type { Recipe, Step, StepOp } from '@/lib/recipe/types'

const STEPS: WizardStep[] = [
  { id: 'session', title: 'Page', hint: 'Which pages, and a browser to look at them in' },
  {
    id: 'extract',
    title: 'Extract',
    hint: 'Pick the values you want, and any clicks or scrolls the page needs first — in the order they happen',
  },
  { id: 'paging', title: 'More', hint: 'How to reach the rest of the results' },
  { id: 'preview', title: 'Preview', hint: 'Run it on this page and look at the data' },
  { id: 'review', title: 'Review', hint: 'What the recipe says, and what the lint makes of it' },
]

/**
 * The guided path to a v2 recipe: the page on the left, the work on the right.
 *
 * The layout is the extension's, and deliberately so. Authoring a recipe is a
 * conversation with a page -- you name a field by looking at the value it
 * holds, you check a selector by watching it light up, you decide about
 * pagination by seeing where the list ends. A stepper that swaps the page out
 * whenever you are not actively clicking breaks that loop at every step, which
 * is why the browser here is a fixed pane that never goes away and the steps
 * move only the panel beside it.
 *
 * This is the front door; `RecipeStudioPage` is still behind it for everything
 * this does not cover -- steps, variants, transform pipelines, per-candidate
 * predicates. Both write through `useRecipeDoc` under the *same* draft key, so
 * "Advanced editor" is a navigation, not an export.
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
  const [items, setItems] = useState<WorkItem[]>([])
  const [paging, setPaging] = useState<PaginationChoice>({ mode: 'none', maxPages: 5 })
  const [hits, setHits] = useState<PathHit[] | null>(null)
  const [preview, setPreview] = useState<PreviewResult[] | null>(null)
  const [previewing, setPreviewing] = useState(false)
  const [applyReveal, setApplyReveal] = useState(true)

  // Fields and reveal steps are two readings of the same ordered list; the
  // order between them is the thing that matters, so it is stored once.
  const drafts = useMemo(
    () => items.flatMap((i) => (i.kind === 'field' ? [i.draft] : [])),
    [items],
  )
  const reveal = useMemo(
    () => items.flatMap((i) => (i.kind === 'action' ? [i.step] : [])),
    [items],
  )

  const picker = usePagePicker(sessionId)
  const execute = useExecuteSession()

  /**
   * Keep the page marked with what has already been taken.
   *
   * This is the feedback that makes picking feel like picking: without it the
   * page looks identical before and after every click, and an author working
   * through a twelve-column table has no way to tell which cells they already
   * have. Reveal-step targets are marked too, in a different colour, because
   * "what gets clicked" and "what gets read" are different claims about the
   * same page.
   *
   * Skipped while a pick is in flight -- the picker draws its own overlay, and
   * two decoration systems fighting over the same element is worse than one.
   */
  useEffect(() => {
    if (!sessionId || picker.status !== 'idle') return
    picker.showHighlights([...toHighlightFields(drafts), ...stepsToHighlightFields(reveal)])
    // `picker.showHighlights` is stable via useCallback; listing the whole
    // picker object would re-fire this on every status change.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId, items, drafts, reveal, picker.status, picker.showHighlights])

  // A preview describes one exact recipe. The moment a field is renamed, a
  // candidate reordered or a reveal step added, the table on screen is about a
  // recipe that no longer exists -- and a stale green table is worse than no
  // table, because it is the one thing here an author is meant to trust.
  useEffect(() => {
    setPreview(null)
  }, [items])

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
        setItems((current) => [
          ...current.filter((i) => i.kind !== 'field'),
          ...picked.map((draft, i) => ({ kind: 'field' as const, id: `f-${Date.now()}-${i}`, draft })),
        ])
        toast({
          title: `${picked.length} field${picked.length === 1 ? '' : 's'} from ${count} rows`,
          description: 'Name them on the Fields step.',
        })
      } else {
        const draft = detailPickToDraft(payload, drafts.map((d) => d.name))
        setItems((current) => [...current, { kind: 'field', id: `f-${Date.now()}`, draft }])
      }
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

  /**
   * Pick the element a reveal step acts on.
   *
   * `single` mode rather than `detail`: this is pointing at a control, not
   * reading a value, so the pagination-grade selector generator is the right
   * one -- it insists on uniqueness and rejects selectors with a page number
   * baked into them.
   */
  async function pickStepTarget(op: StepOp) {
    const payload = await picker.pick('single')
    if (!payload) return
    const selector = payload.itemSelectors?.[0]?.selector ?? payload.containerSelector
    if (!selector) return
    setItems((current) => [
      ...current,
      {
        kind: 'action',
        id: `a-${Date.now()}`,
        step: {
          op,
          target: { kind: 'css', selector },
          // A reveal step is usually optional by nature -- the cookie banner
          // that is not always there, the accordion already open. Failing the
          // whole run because one did not apply is the wrong default.
          on_error: 'continue',
          optional: true,
          args: {},
        },
      },
    ])
  }

  /** An action with no target: a fixed wait. */
  function addPlainAction(op: StepOp) {
    setItems((current) => [
      ...current,
      { kind: 'action', id: `a-${Date.now()}`, step: { op, on_error: 'continue', optional: true, args: {} } },
    ])
  }

  /** Run the bindings against the live page and show what comes back. */
  async function runPreview() {
    if (!sessionId || drafts.length === 0) return
    setPreviewing(true)
    try {
      if (applyReveal && reveal.length > 0) {
        const outcomes = await picker.applySteps(
          reveal.map((step) => ({
            op: step.op,
            selector: step.target?.selector,
            kind: step.target?.kind === 'xpath' ? 'xpath' : 'css',
            text: step.args?.text as string | undefined,
            ms: step.args?.ms as number | undefined,
          })),
        )
        const failed = outcomes.filter((o) => o.status !== 'ok')
        if (failed.length > 0) {
          toast({
            title: `${failed.length} reveal step${failed.length === 1 ? '' : 's'} did not apply`,
            description: failed.map((f) => `${f.op}: ${f.detail ?? f.status}`).join(' · '),
          })
        }
      }
      setPreview(await picker.preview(toPreviewFields(drafts)))
    } catch (err) {
      toast({
        title: 'Preview failed',
        description: err instanceof Error ? err.message : 'Could not reach the page',
        variant: 'destructive',
      })
    } finally {
      setPreviewing(false)
    }
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
    let seen = -1
    setItems((current) =>
      current.map((item) => {
        if (item.kind !== 'field') return item
        seen += 1
        if (seen !== index) return item
        return {
          ...item,
          draft: {
            ...item.draft,
            candidates: withJsonAlternatives(
              item.draft.candidates,
              matches.map((hit) => hitToLocator(hit)),
            ),
          },
        }
      }),
    )
    toast({
      title: `Found in ${matches[0].kind}`,
      description: 'Added above the picked selector — it will be tried first.',
    })
  }

  /** Fold the wizard's state into the document. */
  const built = useMemo(() => {
    // The ordered list compiles into global_setup + field_groups; pagination
    // is appended after, because it only makes sense once the page is in the
    // state the fields expect.
    let recipe = itemsToRecipe(doc, items)
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
  }, [doc, items, paging])

  const issues = useMemo(() => lintRecipe(built), [built])
  const errors = issues.filter((i) => i.severity === 'error').length

  function commitAndOpenEditor() {
    reset(built)
    navigate(recipeId ? `/recipes/${recipeId}/studio` : '/recipes/new/studio')
  }

  const canAdvance =
    (step === 0 && !!sessionId && doc.name.trim().length > 0) ||
    (step === 1 && drafts.length > 0) ||
    step >= 2

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

      {/* The page on the left, the work on the right -- and the page never
          leaves. `min-w-0` on the left cell is what stops the screencast's
          intrinsic width from pushing the panel off-screen in a grid. */}
      <div className="grid min-h-0 flex-1 grid-cols-[minmax(0,1fr)_26rem]">
        <section className="flex min-w-0 flex-col border-r border-border bg-black/5">
          {sessionId ? (
            <LiveViewPanel sessionId={sessionId} />
          ) : (
            <div className="flex h-full items-center justify-center p-6">
              <EmptyState
                title="No page yet"
                description="Open a browser session on the right, and the page you are building against appears here."
              />
            </div>
          )}
        </section>

        <aside className="flex min-h-0 flex-col bg-background">
          <WizardSteps steps={STEPS} current={step} furthest={furthest} onGoTo={setStep} />

          <div className="border-y border-border px-3 py-1.5">
            <p className="text-[11px] text-muted-foreground">{STEPS[step].hint}</p>
          </div>

          <div className="min-h-0 flex-1 overflow-y-auto">
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

            {step === 1 && (
              <StepExtract
                items={items}
                onChange={setItems}
                pickMode={pickMode}
                onPickModeChange={setPickMode}
                status={picker.status}
                onPickField={() => void startPick()}
                onPickAction={(op) => void pickStepTarget(op)}
                onAddPlainAction={addPlainAction}
                onCancel={picker.cancel}
                onRefine={picker.refine}
                onTestSelector={sessionId ? picker.testSelector : undefined}
                onFindInJson={findInJson}
                jsonProbeReady={(hits?.length ?? 0) > 0}
              />
            )}

            {step === 2 && (
              <Step4Pagination
                value={paging}
                onChange={setPaging}
                status={picker.status}
                onPickButton={() => void pickNextButton()}
                onCancel={picker.cancel}
                onRefine={picker.refine}
              />
            )}

            {step === 3 && (
              <StepPreview
                results={preview}
                running={previewing}
                disabled={!sessionId || drafts.length === 0}
                onRun={() => void runPreview()}
                revealCount={reveal.length}
                applyReveal={applyReveal}
                onApplyRevealChange={setApplyReveal}
              />
            )}

            {step === 4 && (
              <div className="flex flex-col gap-3 p-3">
                <div className="rounded-md border border-border">
                  <LintPanel issues={issues} onJump={() => setStep(1)} />
                </div>
                <JsonTab recipe={built} onReplace={(next) => reset(next)} />
              </div>
            )}
          </div>

          <footer className="flex shrink-0 flex-col gap-1.5 border-t border-border px-3 py-2">
            {picker.error && <span className="text-[11px] text-destructive">{picker.error}</span>}
            <div className="flex items-center gap-2">
              {step > 0 && (
                <Button variant="outline" size="sm" onClick={() => setStep(step - 1)}>
                  Back
                </Button>
              )}
              <div className="ml-auto">
                {step < STEPS.length - 1 ? (
                  <Button size="sm" disabled={!canAdvance} onClick={() => goTo(step + 1)}>
                    Next
                    <ArrowRight className="size-3.5" />
                  </Button>
                ) : (
                  <Button size="sm" onClick={commitAndOpenEditor}>
                    Advanced editor
                    <ArrowRight className="size-3.5" />
                  </Button>
                )}
              </div>
            </div>
          </footer>
        </aside>
      </div>
    </div>
  )
}
