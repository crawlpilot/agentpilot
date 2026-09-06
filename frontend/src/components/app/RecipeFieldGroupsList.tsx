import { Badge } from '@/components/ui/badge'
import { EmptyState } from '@/components/app/EmptyState'
import type { RecipeFieldGroup } from '@/lib/api/types'
import { readGroup } from '@/lib/recipe/fieldGroups'

function JsonBlock({ label, value }: { label: string; value: unknown }) {
  return (
    <div>
      <span className="text-xs text-muted-foreground">{label}</span>
      <pre className="mt-1 max-h-64 overflow-auto whitespace-pre-wrap rounded-md border border-border p-2 text-xs">
        {JSON.stringify(value, null, 2)}
      </pre>
    </div>
  )
}

export function RecipeFieldGroupsList({ groups }: { groups: RecipeFieldGroup[] }) {
  if (groups.length === 0) {
    return (
      <EmptyState
        title="No fields discovered yet"
        description="Field groups appear after a successful build. If the build failed (e.g. no LLM key configured), the recipe stays empty."
      />
    )
  }

  return (
    <div className="flex flex-col gap-2">
      {groups.map((group) => {
        const { locators, steps, v2 } = readGroup(group)
        return (
          <details key={group.group_id} className="rounded-md border border-border p-3">
            <summary className="flex cursor-pointer select-none flex-wrap items-center gap-2 text-sm">
              <span className="font-mono text-xs text-muted-foreground">{group.group_id}</span>
              {group.field_names.map((name) => (
                <Badge key={name} variant="outline">
                  {name}
                </Badge>
              ))}
              {/* `rows`, not `array`: a repeat here is most often a `dom_rows`
                  table, whose bindings are keyed by column rather than field. */}
              {group.repeat && <Badge variant="accent">rows</Badge>}
            </summary>
            <div className="mt-3 flex flex-col gap-3">
              {locators && <JsonBlock label={v2 ? 'Bindings' : 'Field locators'} value={locators} />}
              {steps.length > 0 && <JsonBlock label="Reveal steps" value={steps} />}
              {group.repeat && <JsonBlock label="Repeat spec" value={group.repeat} />}
            </div>
          </details>
        )
      })}
    </div>
  )
}
