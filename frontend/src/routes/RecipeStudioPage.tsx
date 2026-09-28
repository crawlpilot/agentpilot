import { useEffect, useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { AlertCircle, AlertTriangle, ArrowLeft, PanelLeft, Save, Undo2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { SchemaPane } from '@/components/app/studio/SchemaPane'
import { PagePane } from '@/components/app/studio/PagePane'
import { StepsTab } from '@/components/app/studio/StepsTab'
import { FieldsTab } from '@/components/app/studio/FieldsTab'
import { TransformsTab } from '@/components/app/studio/TransformsTab'
import { VariantsTab } from '@/components/app/studio/VariantsTab'
import { QualityTab } from '@/components/app/studio/QualityTab'
import { TargetTab } from '@/components/app/studio/TargetTab'
import { JsonTab } from '@/components/app/studio/JsonTab'
import { LintPanel } from '@/components/app/studio/LintPanel'
import { hasDraft, useRecipeDoc } from '@/hooks/useRecipeDoc'
import { useRecipe, useSaveRecipe } from '@/hooks/useRecipes'
import { validationErrors } from '@/lib/api/client'
import { candidatesFor, setCandidates, toExport, SOURCE_PRIORITY } from '@/lib/recipe/document'
import { countBySeverity, lintRecipe } from '@/lib/recipe/lint'
import type { Locator, Recipe, RecipeStatus } from '@/lib/recipe/types'
import { cn } from '@/lib/utils'

const STATUSES: RecipeStatus[] = ['draft', 'approved', 'published']

export function RecipeStudioPage() {
  const { recipeId } = useParams<{ recipeId?: string }>()
  const draftKey = recipeId ?? 'new'
  const { data: existing, isLoading } = useRecipe(recipeId ?? '')

  const { doc, update, undo, reset, hydrate, markSaved, discardDraft, canUndo, dirty, savedAt } =
    useRecipeDoc(draftKey)
  const save = useSaveRecipe()
  // A draft that was already on disk when this page opened. Read once, because
  // the debounced autosave writes one within 400ms of any edit -- asking later
  // would always say yes and the banner would never go away.
  const [shadowingDraft, setShadowingDraft] = useState(() => Boolean(recipeId) && hasDraft(draftKey))
  const [selectedField, setSelectedField] = useState<string | null>(null)
  const [tab, setTab] = useState('fields')
  const [schemaOpen, setSchemaOpen] = useState(false)

  // Load the server's copy once it arrives.
  //
  // This is what `hydrate` is for, and its docstring already described the bug
  // that came of not calling it: "The initial state is computed lazily and only
  // once, so a document that arrives from a fetch -- which is every existing
  // recipe -- could never reach it. That is why opening a built recipe showed an
  // empty editor."
  //
  // The seed it replaces was `emptyRecipe(existing?.data.name)`, which could not
  // work in principle -- `useState(() => ...)` runs on first render, before any
  // fetch resolves -- and made the failure worse when it did nothing: an editor
  // showing the right title and no fields reads as "this recipe is empty", not
  // "this did not load".
  const serverDoc = (existing?.data.document ?? null) as Recipe | null
  useEffect(() => {
    if (serverDoc) hydrate(serverDoc)
  }, [serverDoc, hydrate])

  // Three states used to render the same empty editor, and telling them apart is
  // most of what made this surface feel broken rather than blank.
  const loading = Boolean(recipeId) && isLoading
  const missingDocument = Boolean(recipeId) && !isLoading && existing !== undefined && !serverDoc

  const issues = useMemo(() => lintRecipe(doc), [doc])
  const counts = countBySeverity(issues)
  // Lua is a per-tenant capability in the contract; until the capabilities
  // endpoint reports it, the editor offers it and the lint flags an
  // unflagged script rather than pretending to know the answer.
  const luaEnabled = true

  /**
   * Write the document back as a new version.
   *
   * The header used to read "saved locally -- export from the JSON tab", because
   * `useRecipeDoc` said "There is no `PUT /v1/recipes/{id}` yet". It shipped:
   * `updateRecipe` is `PUT /v1/recipes/{id}` and `useSaveRecipe` has branched
   * create-vs-update on `recipeId` for as long as the wizard has been saving
   * through it. The studio was the only authoring surface that could not save,
   * on the strength of a comment that went stale.
   */
  function saveToServer() {
    if (!recipeId) return
    const built = toExport(doc)
    save.mutate(
      { recipeId, recipe: built },
      {
        onSuccess: () => {
          // The server now holds this document, so it is no longer unsaved
          // work -- and the draft that was shadowing it is not either.
          markSaved(built)
          setShadowingDraft(false)
        },
      },
    )
  }

  /**
   * A 422 from `PUT` carries `validate_document`'s whole list, and the list is
   * the entire value of the error -- the studio is where a hand-edited document
   * is most likely to fail it. `client.validationErrors` reads `details.errors`;
   * `.message` alone is a summary of something the author needs in full.
   */
  const saveError = save.error ? (validationErrors(save.error).join('\n') || 'Save failed') : null

  function addCandidateToSelected(locator: Locator) {
    if (!selectedField) return
    update((r) => {
      const existingCandidates = candidatesFor(r, selectedField)
      return setCandidates(r, selectedField, [
        ...existingCandidates,
        { locator, priority: SOURCE_PRIORITY[locator.kind], verified_on: 0 },
      ])
    })
    setTab('fields')
  }

  // Said plainly rather than rendered as an empty editor. A studio that opens
  // blank while a fetch is still in flight is indistinguishable from one that
  // opened on an empty recipe, and the person cannot tell whether to wait, to
  // start typing, or to go and find the bug.
  if (loading) {
    return (
      <div className="flex h-screen items-center justify-center bg-background">
        <p className="text-sm text-muted-foreground">Loading recipe…</p>
      </div>
    )
  }

  // A recipe with no v2 document is not an empty recipe -- it is a v1 build, or
  // one whose build never finished. Opening an editable-looking blank editor on
  // it invites saving a document over a recipe that never had one.
  if (missingDocument) {
    return (
      <div className="flex h-screen flex-col items-center justify-center gap-3 bg-background p-6 text-center">
        <p className="text-sm font-medium">This recipe has no editable document</p>
        <p className="max-w-md text-sm text-muted-foreground">
          It was built before the v2 document existed, or its build did not finish. Rebuild it
          from the recipe page to get one.
        </p>
        <Button size="sm" variant="outline" asChild>
          <Link to={`/recipes/${recipeId}`}>Back to recipe</Link>
        </Button>
      </div>
    )
  }

  return (
    <div className="flex h-screen flex-col bg-background">
      <header className="flex shrink-0 flex-wrap items-center gap-2 border-b border-border px-3 py-2">
        <Button size="icon" variant="ghost" asChild>
          <Link to={recipeId ? `/recipes/${recipeId}` : '/recipes'} aria-label="Back">
            <ArrowLeft className="size-4" />
          </Link>
        </Button>
        <h1 className="text-sm font-semibold">{doc.name || 'Untitled recipe'}</h1>
        {/* There are two editors and, until now, four vocabularies for them:
            routes called `/wizard` and `/studio`, one button saying "Open in
            studio" and another saying "Advanced editor", docstrings saying
            "front door" and "the studio behind it". Settling on Edit/Advanced
            costs nothing and is most of what makes a two-surface editor
            legible. The routes keep their spellings so existing links survive;
            what a person reads is what changes. */}
        <span className="rounded bg-muted px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
          Advanced
        </span>
        {recipeId && (
          <Button size="sm" variant="ghost" className="h-6 text-xs" asChild>
            <Link to={`/recipes/${recipeId}/wizard`}>Back to guided editor</Link>
          </Button>
        )}

        <Select value={doc.status ?? 'draft'} onValueChange={(v) => update((r) => ({ ...r, status: v as RecipeStatus }))}>
          <SelectTrigger className="h-7 w-32 text-xs">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {STATUSES.map((s) => (
              <SelectItem key={s} value={s}>
                {s}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>

        <div className="flex items-center gap-1.5">
          {counts.error > 0 && (
            <Badge variant="destructive">
              <AlertCircle className="size-3" />
              {counts.error}
            </Badge>
          )}
          {counts.warning > 0 && (
            <Badge variant="warning">
              <AlertTriangle className="size-3" />
              {counts.warning}
            </Badge>
          )}
          {counts.error === 0 && counts.warning === 0 && <Badge variant="success">lint clean</Badge>}
        </div>

        <div className="ml-auto flex items-center gap-2">
          <Button
            size="sm"
            variant={schemaOpen ? 'secondary' : 'ghost'}
            onClick={() => setSchemaOpen((open) => !open)}
            aria-pressed={schemaOpen}
          >
            <PanelLeft className="size-3.5" />
            Schema
          </Button>
          <Button size="sm" variant="ghost" disabled={!canUndo} onClick={undo}>
            <Undo2 className="size-3.5" />
            Undo
          </Button>
          {recipeId && (
            <Button size="sm" disabled={!dirty || save.isPending} onClick={saveToServer}>
              <Save className="size-3.5" />
              {save.isPending ? 'Saving…' : 'Save'}
            </Button>
          )}
          <span className="text-[11px] text-muted-foreground">
            {savedAt
              ? `saved ${new Date(savedAt).toLocaleTimeString()}`
              : dirty
                ? 'unsaved changes'
                : 'up to date'}
          </span>
        </div>
      </header>

      {/* A draft that was already on disk outranks the server copy, and until
          now nothing said so. `hydrate` declines when a draft exists -- rightly,
          since an unsaved draft is work somebody did and silently replacing it
          is the one thing a draft mechanism must never do -- but with no save
          button every visit left one, so a recipe opened once and abandoned was
          shadowed forever with no indication and no way to clear it.
          `discardDraft` existed and was never called. */}
      {shadowingDraft && (
        <div className="flex shrink-0 flex-wrap items-center gap-2 border-b border-border bg-warning/10 px-3 py-1.5">
          <AlertTriangle className="size-3.5 shrink-0 text-warning" />
          <span className="text-xs">
            Showing unsaved local changes from a previous visit, not the saved recipe.
          </span>
          <Button
            size="sm"
            variant="outline"
            className="ml-auto h-6 text-xs"
            onClick={() => {
              discardDraft()
              if (serverDoc) reset(serverDoc, true)
              setShadowingDraft(false)
            }}
          >
            Discard and reload
          </Button>
          <Button
            size="sm"
            variant="ghost"
            className="h-6 text-xs"
            onClick={() => setShadowingDraft(false)}
          >
            Keep editing
          </Button>
        </div>
      )}

      {saveError && (
        <div className="shrink-0 border-b border-border bg-destructive/10 px-3 py-1.5">
          <p className="whitespace-pre-line text-xs text-destructive">{saveError}</p>
        </div>
      )}

      {/* The page on the left, the document on the right.
          It used to sit in the middle, with the output schema to its left and
          the editor tabs to its right -- which put the two things you edit
          together on opposite sides of the one thing you look at, and left the
          page narrower than either. Every authoring action is a conversation
          with the page, so the page gets one uninterrupted side and everything
          that edits the document is stacked on the other. */}
      {/* The schema pane collapses, and starts collapsed.
          Both side panels were permanent at 20rem + 34rem -- 54rem, about 864px
          -- so on a 1440px screen the live page got roughly 576px, while the
          wizard's single 26rem panel leaves it about 1024px. The surface whose
          whole premise is that authoring is a conversation with the page gave
          that page a bit over half the room the simpler screen does, and did it
          by showing the output schema whether or not anyone was editing it.
          Closed by default, one click away, and the page gets its width back. */}
      <div
        className={cn(
          'grid min-h-0 flex-1',
          schemaOpen
            ? 'grid-cols-[minmax(0,1fr)_20rem_34rem]'
            : 'grid-cols-[minmax(0,1fr)_34rem]',
        )}
      >
        <main className="min-w-0 border-r border-border">
          <PagePane selectedField={selectedField} onAddCandidate={addCandidateToSelected} />
        </main>

        {schemaOpen && (
          <aside className="min-h-0 border-r border-border">
            <SchemaPane
              recipe={doc}
              update={update}
              selectedField={selectedField}
              onSelectField={setSelectedField}
            />
          </aside>
        )}

        <aside className="flex min-h-0 flex-col">
          <Tabs value={tab} onValueChange={setTab} className="flex min-h-0 flex-1 flex-col">
            {/* Four tabs, not seven.
                The seven were presented as peers and are not: `fields` is where
                nearly all editing happens, while target, variants and quality
                are set once and rarely revisited. Four is scannable; seven is a
                menu you have to read before you can use it. Nothing was removed
                -- the three now sit together under Settings, which is also a
                truer description of what they are. */}
            <TabsList className="m-2 w-fit shrink-0">
              {['fields', 'steps', 'transforms', 'settings', 'json'].map((t) => (
                <TabsTrigger key={t} value={t} className="capitalize">
                  {t}
                </TabsTrigger>
              ))}
            </TabsList>

            <div className="min-h-0 flex-1 overflow-y-auto px-3 pb-3">
              <TabsContent value="steps">
                <StepsTab recipe={doc} update={update} />
              </TabsContent>
              <TabsContent value="fields">
                <FieldsTab
                  recipe={doc}
                  update={update}
                  selectedField={selectedField}
                  onSelectField={setSelectedField}
                  luaEnabled={luaEnabled}
                />
              </TabsContent>
              <TabsContent value="transforms">
                <TransformsTab recipe={doc} update={update} luaEnabled={luaEnabled} />
              </TabsContent>
              <TabsContent value="settings" className="flex flex-col gap-6">
                <section className="flex flex-col gap-2">
                  <h2 className="text-xs font-semibold text-muted-foreground">
                    Which pages this recipe runs on
                  </h2>
                  <TargetTab recipe={doc} update={update} />
                </section>
                <section className="flex flex-col gap-2">
                  <h2 className="text-xs font-semibold text-muted-foreground">
                    Page variants
                  </h2>
                  <VariantsTab recipe={doc} update={update} />
                </section>
                <section className="flex flex-col gap-2">
                  <h2 className="text-xs font-semibold text-muted-foreground">
                    Quality checks
                  </h2>
                  <QualityTab recipe={doc} update={update} />
                </section>
              </TabsContent>
              <TabsContent value="json">
                <JsonTab recipe={doc} onReplace={(next) => reset(next)} />
              </TabsContent>
            </div>
          </Tabs>

          <div
            className={cn(
              'max-h-56 shrink-0 overflow-y-auto border-t border-border',
              counts.error > 0 && 'bg-destructive/5',
            )}
          >
            <LintPanel issues={issues} onJump={setTab} />
          </div>
        </aside>
      </div>
    </div>
  )
}
