import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { ArrowLeft, ArrowRight, SlidersHorizontal } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { EmptyState } from '@/components/app/EmptyState'
import { LiveViewPanel } from '@/components/app/LiveViewPanel'
import { WizardSteps, type WizardStep } from '@/components/app/wizard/WizardSteps'
import { Step1Session } from '@/components/app/wizard/Step1Session'
import { StepExtract } from '@/components/app/wizard/StepExtract'
import { Step4Pagination, type PaginationChoice } from '@/components/app/wizard/Step4Pagination'
import { StepPreview } from '@/components/app/wizard/StepPreview'
import { StepSave } from '@/components/app/wizard/StepSave'
import { StepValidate } from '@/components/app/wizard/StepValidate'
import { usePagePicker } from '@/hooks/usePagePicker'
import { useRecipeDoc } from '@/hooks/useRecipeDoc'
import { useExecuteSession } from '@/hooks/useExecuteSession'
import { useRecipe, useSaveRecipe } from '@/hooks/useRecipes'
import { useRecipeValidation } from '@/hooks/useRecipeValidation'
import { useToast } from '@/components/ui/toast'
import { emptyRecipe, toExport } from '@/lib/recipe/document'
import { lintRecipe } from '@/lib/recipe/lint'
import { validationErrors } from '@/lib/api/client'
import { buildValidationPlan, structuredOnlyFields } from '@/lib/recipe/validatePlan'
import {
  detailPickToDraft,
  itemsToRecipe,
  jsonHitToDraft,
  listPickToDrafts,
  stepsToHighlightFields,
  toLocator,
  toHighlightFields,
  toPreviewFields,
  toPreviewRowsFields,
  withJsonAlternatives,
  withStructuredPreview,
  type FieldDraft,
  type WorkItem,
} from '@/lib/recipe/fromPick'
import { STRUCTURED_DATA_ACTION, findPaths, flattenProbe, hitToLocator, parseProbe, type PathHit, type ProbeResult } from '@/lib/recipe/probe'
import type { PickerMode } from '@/lib/picker/protocol'
import type { PreviewResult, PreviewRowsResult } from '@/lib/picker/preview'
import type { TemplateVisibility } from '@/lib/api/types'
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
  { id: 'save', title: 'Save', hint: 'Check the lint, say what this recipe is for, and keep it' },
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
  const { doc, update, reset, hydrate, markSaved } = useRecipeDoc(draftKey, seed)

  // Load what the server holds for this recipe. Without this the editor opened
  // empty for every existing recipe -- including every one the onboarding agent
  // had just built, which is the whole point of handing it over for review.
  const existing = useRecipe(recipeId ?? '')
  useEffect(() => {
    const document = existing.data?.data?.document
    if (document) hydrate(document as unknown as Recipe)
  }, [existing.data, hydrate])
  // True only while an EXISTING recipe's document is still in flight. A brand
  // new recipe has nothing to wait for.
  const loadingExisting = Boolean(recipeId) && existing.data === undefined

  const [step, setStep] = useState(0)
  const [furthest, setFurthest] = useState(0)
  const [sessionId, setSessionId] = useState<string | null>(null)
  const [pickMode, setPickMode] = useState<Exclude<PickerMode, 'single'>>('list')
  const [items, setItems] = useState<WorkItem[]>([])
  // The field just added, so it opens on its refinement controls. Choosing the
  // shape is the intended next action after a broad pick, and making an author
  // find a chevron to discover that is most of why per-column editing goes
  // unused.
  const [justPicked, setJustPicked] = useState<string | null>(null)
  const [paging, setPaging] = useState<PaginationChoice>({ mode: 'none', maxPages: 5 })
  const [hits, setHits] = useState<PathHit[] | null>(null)
  // The probe payload itself, not just its flattened hits: the preview needs
  // it to resolve a field bound to a JSON path, which the DOM reader cannot.
  const [probe, setProbe] = useState<ProbeResult | null>(null)
  const [preview, setPreview] = useState<PreviewResult[] | null>(null)
  const [previewRows, setPreviewRows] = useState<PreviewRowsResult[]>([])
  const [previewing, setPreviewing] = useState(false)
  const [applyReveal, setApplyReveal] = useState(true)
  // Pages to build and test against. Wizard scaffolding, not recipe content:
  // a saved recipe is applied to whatever URLs a caller submits at runtime, so
  // the page it was authored on is not part of it. See `lib/recipe/lint.ts`.
  const [workUrls, setWorkUrls] = useState<string[]>([])
  const [validateUrl, setValidateUrl] = useState('')
  const [pageType, setPageType] = useState('')
  const [visibility, setVisibility] = useState<TemplateVisibility>('private')
  // The id the first save returns, so a second save is an update rather than a
  // duplicate recipe.
  const [savedId, setSavedId] = useState<string | null>(recipeId ?? null)
  const [saved, setSaved] = useState<{ recipeId: string; version: number; warnings: string[] } | null>(null)

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
  const save = useSaveRecipe()
  const validation = useRecipeValidation(sessionId, picker)

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
    setPreviewRows([])
    // A validation run describes one exact recipe too; leaving a stale green
    // result on screen after an edit is the one thing here nobody should
    // trust and everybody would.
    validation.clear()
    // `validation.clear` is stable via useCallback.
    // eslint-disable-next-line react-hooks/exhaustive-deps
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
      { sessionId, actions: [{ ...STRUCTURED_DATA_ACTION }] },
      {
        onSuccess: (result) => {
          // `extracts[0]`, not `js_returns` -- the same payload
          // `PageReader.structured_data()` parses, so a path offered here is
          // one replay can resolve.
          const parsed = parseProbe(result.extracts?.[0])
          setProbe(parsed)
          setHits(flattenProbe(parsed))
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
        const ids = picked.map((_, i) => `f-${Date.now()}-${i}`)
        setItems((current) => [
          ...current.filter((i) => i.kind !== 'field'),
          ...picked.map((draft, i) => ({ kind: 'field' as const, id: ids[i], draft })),
        ])
        setJustPicked(ids[0])
        toast({
          title: `${picked.length} field${picked.length === 1 ? '' : 's'} from ${count} rows`,
          description: 'Name them on the Fields step.',
        })
      } else {
        const draft = detailPickToDraft(payload, drafts.map((d) => d.name))
        const id = `f-${Date.now()}`
        setItems((current) => [...current, { kind: 'field', id, draft }])
        setJustPicked(id)
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
   * `detail` mode with a `click` preference, which is what the extension's
   * "Add Click Action" uses. NOT `single`: that is the *pagination* strategy,
   * and it does two things that are right for a next-button and wrong here --
   * it auto-scrolls off hunting for something that looks like one, and its
   * generator rejects any selector containing a digit, so an accordion toggle
   * on `[data-index="2"]` comes back with no usable selector at all.
   */
  async function pickStepTarget(op: StepOp) {
    const payload = await picker.pick('detail', 'click')
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
          target: toLocator(selector),
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
      // Scalars and tables are two different reads: a table resolves each
      // column *relative to its row*, which is what keeps rows aligned.
      const [scalars, tables] = await Promise.all([
        picker.preview(toPreviewFields(drafts)),
        picker.previewRows(toPreviewRowsFields(drafts)),
      ])
      // The DOM reader cannot answer for a field bound to page JSON, and a
      // JSON-first recipe is the shape the studio recommends -- so those are
      // resolved here, against the data the probe already holds.
      setPreview(withStructuredPreview(scalars, drafts, probe))
      setPreviewRows(tables)
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

  /** Define a field straight from a path into the page's embedded JSON. */
  function addJsonField(hit: PathHit) {
    setItems((current) => [
      ...current,
      { kind: 'field', id: `j-${Date.now()}`, draft: jsonHitToDraft(hit, drafts.map((d) => d.name)) },
    ])
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
        target: toLocator(paging.selector),
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

  function saveRecipe() {
    // Never write over a recipe whose current contents have not arrived yet.
    // The editor seeds an empty document and hydrates from the server a beat
    // later, so a save in that window would replace a working scraper with a
    // blank one and record it as a new version. `loading` is only true when
    // there is a recipe id to load.
    if (loadingExisting) {
      toast({
        title: 'Still loading this recipe',
        description: 'Saving now would overwrite it with an empty document.',
      })
      return
    }
    setSaved(null)
    save.mutate(
      {
        recipeId: savedId,
        recipe: toExport(built),
        page_type: pageType.trim() || null,
        template_visibility: visibility,
      },
      {
        onSuccess: (res) => {
          setSavedId(res.recipe_id)
          setSaved({ recipeId: res.recipe_id, version: res.version, warnings: res.warnings })
          // The document is now what the server holds, so the draft is no
          // longer unsaved work.
          markSaved(built)
        },
      },
    )
  }

  /**
   * The save failure, as the author needs to read it.
   *
   * A 422 from `POST /v1/recipes/v2` carries `validate_document`'s whole list in
   * `details.errors`; `message` only summarises the first few. Anything else --
   * a 401, a network failure -- has no list and its message is the message.
   */
  function saveError(err: unknown): string | null {
    if (!err) return null
    const reasons = validationErrors(err)
    if (reasons.length > 0) return reasons.join('\n')
    return err instanceof Error ? err.message : String(err)
  }

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
                workUrls={workUrls}
                onWorkUrlsChange={setWorkUrls}
                name={doc.name}
                onNameChange={(name) => update((r) => ({ ...r, name }))}
              />
            )}

            {step === 1 && (
              <StepExtract
                items={items}
                onChange={setItems}
                autoExpandId={justPicked}
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
                jsonHits={hits}
                jsonLoading={execute.isPending}
                onProbeJson={() => { setHits(null); probeJson() }}
                onAddJsonField={addJsonField}
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
              <div className="flex flex-col">
              <StepPreview
                results={preview}
                rowResults={previewRows}
                running={previewing}
                disabled={!sessionId || drafts.length === 0}
                onRun={() => void runPreview()}
                revealCount={reveal.length}
                applyReveal={applyReveal}
                onApplyRevealChange={setApplyReveal}
              />
              <div className="px-3 pb-3">
                <StepValidate
                  urls={workUrls}
                  url={validateUrl || workUrls[0] || ''}
                  onUrlChange={setValidateUrl}
                  authoredUrl={workUrls[0] ?? null}
                  running={validation.running}
                  progress={validation.progress}
                  result={validation.result}
                  error={validation.error}
                  disabled={!sessionId || drafts.length === 0}
                  structuredOnly={structuredOnlyFields(built)}
                  onRun={() =>
                    void validation.validate(
                      validateUrl || workUrls[0] || '',
                      buildValidationPlan(built),
                    )
                  }
                />
              </div>
              </div>
            )}

            {step === 4 && (
              <StepSave
                recipe={built}
                issues={issues}
                onReplace={(next) => reset(next)}
                onJump={() => setStep(1)}
                pageType={pageType}
                onPageTypeChange={setPageType}
                visibility={visibility}
                onVisibilityChange={setVisibility}
                onSave={saveRecipe}
                saving={save.isPending}
                saved={saved}
                // Every validation reason, not the three the server's summary
                // fits into `message`. A 422 here is a refusal with a list, and
                // fixing one item per save round trip is the thing this avoids.
                error={saveError(save.error)}
              />
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
                  <Button size="sm" variant="outline" onClick={commitAndOpenEditor}>
                    <SlidersHorizontal className="size-3.5" />
                    Advanced editor
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
