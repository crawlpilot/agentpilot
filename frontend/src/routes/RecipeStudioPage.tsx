import { useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { AlertCircle, AlertTriangle, ArrowLeft, Undo2 } from 'lucide-react'
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
import { JsonTab } from '@/components/app/studio/JsonTab'
import { LintPanel } from '@/components/app/studio/LintPanel'
import { useRecipeDoc } from '@/hooks/useRecipeDoc'
import { useRecipe } from '@/hooks/useRecipes'
import { candidatesFor, emptyRecipe, setCandidates, SOURCE_PRIORITY } from '@/lib/recipe/document'
import { countBySeverity, lintRecipe } from '@/lib/recipe/lint'
import type { Locator, Recipe, RecipeStatus } from '@/lib/recipe/types'
import { cn } from '@/lib/utils'

const STATUSES: RecipeStatus[] = ['draft', 'approved', 'published']

export function RecipeStudioPage() {
  const { recipeId } = useParams<{ recipeId?: string }>()
  const draftKey = recipeId ?? 'new'
  const { data: existing } = useRecipe(recipeId ?? '')

  const seed = useMemo<Recipe>(
    () => emptyRecipe(existing?.data.name ?? ''),
    [existing?.data.name],
  )
  const { doc, update, undo, reset, canUndo } = useRecipeDoc(draftKey, seed)
  const [selectedField, setSelectedField] = useState<string | null>(null)
  const [tab, setTab] = useState('fields')

  const issues = useMemo(() => lintRecipe(doc), [doc])
  const counts = countBySeverity(issues)
  // Lua is a per-tenant capability in the contract; until the capabilities
  // endpoint reports it, the editor offers it and the lint flags an
  // unflagged script rather than pretending to know the answer.
  const luaEnabled = true

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

  return (
    <div className="flex h-screen flex-col bg-background">
      <header className="flex shrink-0 flex-wrap items-center gap-2 border-b border-border px-3 py-2">
        <Button size="icon" variant="ghost" asChild>
          <Link to={recipeId ? `/recipes/${recipeId}` : '/recipes'} aria-label="Back">
            <ArrowLeft className="size-4" />
          </Link>
        </Button>
        <h1 className="text-sm font-semibold">{doc.name || 'Untitled recipe'}</h1>

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
          <Button size="sm" variant="ghost" disabled={!canUndo} onClick={undo}>
            <Undo2 className="size-3.5" />
            Undo
          </Button>
          <span className="text-[11px] text-muted-foreground">
            saved locally -- export from the JSON tab
          </span>
        </div>
      </header>

      {/* The page on the left, the document on the right.
          It used to sit in the middle, with the output schema to its left and
          the editor tabs to its right -- which put the two things you edit
          together on opposite sides of the one thing you look at, and left the
          page narrower than either. Every authoring action is a conversation
          with the page, so the page gets one uninterrupted side and everything
          that edits the document is stacked on the other. */}
      <div className="grid min-h-0 flex-1 grid-cols-[minmax(0,1fr)_20rem_34rem]">
        <main className="min-w-0 border-r border-border">
          <PagePane selectedField={selectedField} onAddCandidate={addCandidateToSelected} />
        </main>

        <aside className="min-h-0 border-r border-border">
          <SchemaPane
            recipe={doc}
            update={update}
            selectedField={selectedField}
            onSelectField={setSelectedField}
          />
        </aside>

        <aside className="flex min-h-0 flex-col">
          <Tabs value={tab} onValueChange={setTab} className="flex min-h-0 flex-1 flex-col">
            <TabsList className="m-2 w-fit shrink-0">
              {['steps', 'fields', 'transforms', 'variants', 'quality', 'json'].map((t) => (
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
              <TabsContent value="variants">
                <VariantsTab recipe={doc} update={update} />
              </TabsContent>
              <TabsContent value="quality">
                <QualityTab recipe={doc} update={update} />
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
