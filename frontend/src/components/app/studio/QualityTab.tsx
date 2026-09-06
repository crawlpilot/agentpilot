import { Input } from '@/components/ui/input'
import { Badge } from '@/components/ui/badge'
import { AssertionList } from '@/components/app/studio/AssertionEditor'
import { candidatesFor, updateField, updateGroup } from '@/lib/recipe/document'
import type { Recipe } from '@/lib/recipe/types'

interface Props {
  recipe: Recipe
  update: (fn: (r: Recipe) => Recipe) => void
}

export function QualityTab({ recipe, update }: Props) {
  return (
    <div className="flex flex-col gap-5">
      <section className="flex flex-col gap-2">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Per-field assertions</h3>
        <p className="text-[11px] text-muted-foreground">
          A selector that drifts onto the wrong element keeps producing well-typed values indefinitely. Resolution
          checks cannot see that; a range, a pattern or a cross-source comparison can.
        </p>
        {Object.entries(recipe.fields).map(([name, spec]) => {
          const kinds = new Set(candidatesFor(recipe, name).map((c) => c.locator.kind))
          return (
            <div key={name} className="rounded-lg border border-border p-2">
              <div className="mb-1.5 flex flex-wrap items-center gap-2">
                <span className="font-mono text-xs font-semibold">{name}</span>
                {spec.required && <Badge variant="outline">required</Badge>}
                {kinds.size > 1 && (
                  <Badge variant="accent" title="Two candidate kinds -- cross_source_agrees costs one extra read here">
                    {kinds.size} sources
                  </Badge>
                )}
              </div>
              <AssertionList
                assertions={spec.assertions ?? []}
                onChange={(assertions) => update((r) => updateField(r, name, { ...spec, assertions }))}
              />
            </div>
          )
        })}
      </section>

      <section className="flex flex-col gap-2">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Row expectations</h3>
        <p className="text-[11px] text-muted-foreground">
          A run that captured 8 of 40 rows and one that captured all 40 look identical without these. Completeness is
          the failure nobody notices, because nothing about a short result looks wrong.
        </p>
        {recipe.field_groups.map((group) => (
          <div key={group.group_id} className="flex flex-wrap items-center gap-3 rounded-lg border border-border p-2">
            <span className="font-mono text-xs font-semibold">{group.group_id}</span>
            {group.repeat ? (
              <Badge variant="outline">repeat: {group.repeat.kind}, max {group.repeat.max_iterations}</Badge>
            ) : (
              <span className="text-[11px] text-muted-foreground">single row</span>
            )}
            <label className="ml-auto flex items-center gap-1.5 text-[11px] text-muted-foreground">
              min rows
              <Input
                type="number"
                className="h-7 w-20 text-xs"
                value={group.expect?.min_rows ?? ''}
                onChange={(e) =>
                  update((r) =>
                    updateGroup(r, group.group_id, {
                      expect: { ...group.expect, min_rows: e.target.value ? Number(e.target.value) : null },
                    }),
                  )
                }
              />
            </label>
            <label className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
              max rows
              <Input
                type="number"
                className="h-7 w-20 text-xs"
                value={group.expect?.max_rows ?? ''}
                onChange={(e) =>
                  update((r) =>
                    updateGroup(r, group.group_id, {
                      expect: { ...group.expect, max_rows: e.target.value ? Number(e.target.value) : null },
                    }),
                  )
                }
              />
            </label>
          </div>
        ))}
      </section>

      <section className="flex flex-col gap-2">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Trailing metrics</h3>
        <p className="text-[11px] text-muted-foreground">
          Per-field fill rate, null rate and candidate-demotion frequency over a rolling window are what turn "a field
          quietly stopped resolving" into an alert. They need{' '}
          <code className="font-mono">GET /v1/recipes/{'{id}'}/metrics</code>, which does not exist yet -- see
          docs/recipe-studio.md.
        </p>
      </section>
    </div>
  )
}
