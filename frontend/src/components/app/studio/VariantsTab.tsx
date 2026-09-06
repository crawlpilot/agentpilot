import { useState } from 'react'
import { Check, Plus, Trash2, X } from 'lucide-react'
import { Input } from '@/components/ui/input'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { PredicateList } from '@/components/app/studio/PredicateEditor'
import { newVariant, removeAt, replaceAt } from '@/lib/recipe/document'
import { matcherMatches, targetAccepts } from '@/lib/recipe/urlmatch'
import type { PageVariant, Recipe } from '@/lib/recipe/types'
import { cn } from '@/lib/utils'

interface Props {
  recipe: Recipe
  update: (fn: (r: Recipe) => Recipe) => void
}

export function VariantsTab({ recipe, update }: Props) {
  const variants = recipe.variants ?? []
  const setVariants = (next: PageVariant[]) => update((r) => ({ ...r, variants: next }))

  return (
    <div className="flex flex-col gap-4">
      <UrlTester recipe={recipe} />

      <p className="text-[11px] text-muted-foreground">
        Variants are checked once after global setup, highest priority first, and the first one whose predicates all
        hold wins. If none match the recipe still runs -- only unscoped candidates apply. That is a degraded run, not a
        failed one, and it is worth knowing about.
      </p>

      {[...variants]
        .map((variant, index) => ({ variant, index }))
        .sort((a, b) => (a.variant.priority ?? 100) - (b.variant.priority ?? 100))
        .map(({ variant, index }) => (
          <section key={index} className="rounded-lg border border-border p-2">
            <div className="mb-1.5 flex flex-wrap items-center gap-2">
              <Input
                className="h-7 w-48 font-mono text-xs"
                defaultValue={variant.variant_id}
                onBlur={(e) => {
                  const id = e.target.value.trim()
                  if (id && id !== variant.variant_id) setVariants(replaceAt(variants, index, { ...variant, variant_id: id }))
                }}
              />
              <label className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
                priority
                <Input
                  type="number"
                  className="h-7 w-20 text-xs"
                  value={variant.priority ?? 100}
                  onChange={(e) => setVariants(replaceAt(variants, index, { ...variant, priority: Number(e.target.value) }))}
                />
              </label>
              <Input
                className="h-7 w-64 text-xs"
                placeholder="label -- what this layout is"
                value={variant.label ?? ''}
                onChange={(e) => setVariants(replaceAt(variants, index, { ...variant, label: e.target.value || null }))}
              />
              <Button
                size="icon"
                variant="ghost"
                className="ml-auto size-6"
                onClick={() => setVariants(removeAt(variants, index))}
              >
                <Trash2 className="size-3.5" />
              </Button>
            </div>
            <PredicateList
              predicates={variant.detect}
              onChange={(detect) => setVariants(replaceAt(variants, index, { ...variant, detect }))}
              label="add detect predicate"
              hint="no predicates -- this variant can never be selected"
            />
          </section>
        ))}

      <Button size="sm" variant="outline" className="w-fit" onClick={() => setVariants([...variants, newVariant(variants)])}>
        <Plus className="size-3.5" />
        add variant
      </Button>
    </div>
  )
}

/**
 * "Will this recipe accept this URL, and which matcher decided?"
 *
 * Only the URL guard is testable without a browser -- `selector_present` and
 * friends need a live page, and the tester says so rather than reporting a
 * confident half-answer.
 */
function UrlTester({ recipe }: { recipe: Recipe }) {
  const [url, setUrl] = useState(recipe.sample_urls[0] ?? '')
  const accepted = url ? targetAccepts(recipe.target, url) : null

  return (
    <div className="flex flex-col gap-1.5 rounded-lg border border-border p-2">
      <span className="text-xs font-semibold">URL guard tester</span>
      <div className="flex items-center gap-2">
        <Input
          className="h-7 flex-1 font-mono text-xs"
          placeholder="https://…"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
        />
        {accepted !== null && (
          <Badge variant={accepted ? 'success' : 'destructive'} className="shrink-0">
            {accepted ? <Check className="size-3" /> : <X className="size-3" />}
            {accepted ? 'accepted' : 'rejected'}
          </Badge>
        )}
      </div>
      {url && recipe.target.match.length > 0 && (
        <div className="flex flex-col gap-0.5">
          {recipe.target.match.map((matcher, i) => {
            const hit = matcherMatches(matcher, url)
            return (
              <div key={i} className={cn('font-mono text-[11px]', hit ? 'text-success' : 'text-muted-foreground')}>
                {hit ? '✓' : '·'} {matcher.kind} {matcher.pattern}
              </div>
            )
          })}
        </div>
      )}
      {url && recipe.target.match.length === 0 && (
        <span className="text-[11px] text-muted-foreground">No matchers, so every URL is accepted.</span>
      )}
      <span className="text-[11px] text-muted-foreground">
        Variant detection is not testable here -- its predicates read the loaded page, not the URL.
      </span>
    </div>
  )
}
