import { Badge } from '@/components/ui/badge'
import { TransformList } from '@/components/app/studio/TransformEditor'
import { candidatesFor, describeTypeSpec, updateField } from '@/lib/recipe/document'
import type { Recipe } from '@/lib/recipe/types'
import { EmptyState } from '@/components/app/EmptyState'

interface Props {
  recipe: Recipe
  update: (fn: (r: Recipe) => Recipe) => void
  luaEnabled: boolean
}

export function TransformsTab({ recipe, update, luaEnabled }: Props) {
  const fields = Object.entries(recipe.fields)
  if (fields.length === 0) {
    return <EmptyState title="No fields yet" description="Declare a field in the schema pane to give it a pipeline." />
  }

  return (
    <div className="flex flex-col gap-4">
      <p className="text-[11px] text-muted-foreground">
        The pipeline runs top to bottom on whatever the winning candidate returned, then the declared type is applied.
        A candidate can override this list entirely -- open it in the Fields tab.
      </p>

      {fields.map(([name, spec]) => {
        const overrides = candidatesFor(recipe, name).filter((c) => c.transform != null).length
        return (
          <section key={name} className="rounded-lg border border-border p-2">
            <div className="mb-1.5 flex flex-wrap items-center gap-2">
              <span className="font-mono text-xs font-semibold">{name}</span>
              <Badge variant="outline">{describeTypeSpec(spec.type)}</Badge>
              {overrides > 0 && (
                <Badge variant="warning" title="These candidates ignore the pipeline below">
                  {overrides} candidate override{overrides > 1 ? 's' : ''}
                </Badge>
              )}
            </div>
            <TransformList
              transforms={spec.transform ?? []}
              onChange={(transform) => update((r) => updateField(r, name, { ...spec, transform }))}
              group={`fieldtx:${name}`}
              luaEnabled={luaEnabled}
            />
          </section>
        )
      })}

      {!luaEnabled && (
        <p className="text-[11px] text-muted-foreground">
          Lua transforms are disabled for this tenant. They are the escape hatch for parsing the declarative ops cannot
          express -- "Length: 120 cm / 47.2 in" into two numbers -- and run sandboxed with no page, network, clock or
          filesystem access.
        </p>
      )}
    </div>
  )
}
