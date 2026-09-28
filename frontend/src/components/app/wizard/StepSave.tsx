import { AlertTriangle, Check, Loader2, Save } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { LintPanel } from '@/components/app/studio/LintPanel'
import { JsonTab } from '@/components/app/studio/JsonTab'
import type { LintIssue } from '@/lib/recipe/lint'
import type { Recipe } from '@/lib/recipe/types'
import type { TemplateVisibility } from '@/lib/api/types'

interface Props {
  recipe: Recipe
  issues: LintIssue[]
  onReplace: (next: Recipe) => void
  onJump: () => void

  pageType: string
  onPageTypeChange: (value: string) => void
  visibility: TemplateVisibility
  onVisibilityChange: (value: TemplateVisibility) => void

  onSave: () => void
  saving: boolean
  saved: { recipeId: string; version: number; warnings: string[] } | null
  error: string | null
}

/**
 * Common page kinds, offered as a starting point rather than a closed set.
 *
 * The taxonomy grows with the sites people scrape, and `page_type` is stored
 * as free text for that reason -- an author who needs `store-locator` should
 * be able to type it rather than file a schema change.
 */
const PAGE_TYPES = ['pdp', 'plp', 'category', 'search', 'listing', 'article', 'profile']

const VISIBILITY: { value: TemplateVisibility; label: string; hint: string }[] = [
  { value: 'private', label: 'Private', hint: 'Only this recipe. Not offered as a template.' },
  { value: 'tenant', label: 'My organisation', hint: 'Anyone in this tenant can start from it.' },
  { value: 'public', label: 'Public', hint: 'Listed in the shared scraper marketplace.' },
]

export function StepSave({
  recipe,
  issues,
  onReplace,
  onJump,
  pageType,
  onPageTypeChange,
  visibility,
  onVisibilityChange,
  onSave,
  saving,
  saved,
  error,
}: Props) {
  const errors = issues.filter((i) => i.severity === 'error')

  return (
    <div className="flex flex-col gap-3 p-3">
      <div className="rounded-md border border-border">
        <LintPanel issues={issues} onJump={onJump} />
      </div>

      <div className="flex flex-col gap-2 rounded-md border border-border p-2.5">
        <p className="text-[10px] uppercase tracking-wide text-muted-foreground">
          Scraper marketplace
        </p>
        <p className="text-[11px] leading-snug text-muted-foreground">
          What this recipe is <em>for</em>, so it can be found by page kind rather than by name.
          Published recipes are applied to whatever URLs a caller submits &mdash; the pages you
          built against are not saved with it.
        </p>

        <div className="flex flex-col gap-1.5">
          <Label htmlFor="page-type">Page type</Label>
          <Input
            id="page-type"
            list="page-types"
            value={pageType}
            onChange={(e) => onPageTypeChange(e.target.value)}
            placeholder="pdp"
            className="h-7 font-mono text-xs"
          />
          <datalist id="page-types">
            {PAGE_TYPES.map((t) => (
              <option key={t} value={t} />
            ))}
          </datalist>
        </div>

        <div className="flex flex-col gap-1.5">
          <Label htmlFor="visibility">Who can start from this</Label>
          <Select value={visibility} onValueChange={(v) => onVisibilityChange(v as TemplateVisibility)}>
            <SelectTrigger id="visibility" className="h-7 text-xs">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {VISIBILITY.map((v) => (
                <SelectItem key={v.value} value={v.value} title={v.hint}>
                  {v.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <p className="text-[10px] text-muted-foreground">
            {VISIBILITY.find((v) => v.value === visibility)?.hint}
          </p>
        </div>
      </div>

      <div className="flex flex-col gap-1.5">
        <Button onClick={onSave} disabled={saving || errors.length > 0}>
          {saving ? <Loader2 className="size-3.5 animate-spin" /> : <Save className="size-3.5" />}
          {saving ? 'Saving…' : saved ? 'Save new version' : 'Save recipe'}
        </Button>
        {errors.length > 0 && (
          <p className="text-[11px] text-destructive">
            {errors.length} error{errors.length === 1 ? '' : 's'} above &mdash; the server rejects
            the same ones, so they are fixed here rather than discovered on save.
          </p>
        )}
        {/* `whitespace-pre-line` because a 422 is a LIST of validation
            reasons joined by newlines, not one sentence -- see
            `RecipeWizardPage::saveError`. */}
        {error && (
          <p className="whitespace-pre-line text-[11px] text-destructive">{error}</p>
        )}
      </div>

      {saved && (
        <div className="flex flex-col gap-1 rounded-md border border-success/40 bg-success/10 p-2.5">
          <p className="flex items-center gap-1.5 text-[11px] font-medium text-success">
            <Check className="size-3.5" />
            Saved as version {saved.version}
          </p>
          <p className="font-mono text-[10px] text-muted-foreground">{saved.recipeId}</p>
          {saved.warnings.length > 0 && (
            <ul className="flex flex-col gap-0.5 pt-1">
              {saved.warnings.map((w) => (
                <li key={w} className="flex items-start gap-1.5 text-[10px] text-warning">
                  <AlertTriangle className="mt-0.5 size-3 shrink-0" />
                  {w}
                </li>
              ))}
            </ul>
          )}
          <p className="pt-0.5 text-[10px] text-muted-foreground">
            Versions are append-only, so every save is recoverable.
          </p>
        </div>
      )}

      <div className="flex items-center gap-2">
        <p className="text-[10px] uppercase tracking-wide text-muted-foreground">The document</p>
        <Badge variant="outline">v{recipe.version ?? 1}</Badge>
      </div>
      <JsonTab recipe={recipe} onReplace={onReplace} />
    </div>
  )
}
