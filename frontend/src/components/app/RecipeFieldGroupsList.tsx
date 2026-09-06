import { Badge } from '@/components/ui/badge'
import { EmptyState } from '@/components/app/EmptyState'
import type { RecipeFieldGroup } from '@/lib/api/types'

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

interface ReadableGroup {
  /** Where each field's value comes from, whichever key the recipe used. */
  locators: Record<string, unknown> | null
  /** Reveal steps, whichever key the recipe used. Never undefined. */
  steps: Record<string, unknown>[]
  /** True for the v2 shape, whose bindings key a table by COLUMN. */
  v2: boolean
}

/**
 * Read a group without caring which schema wrote it.
 *
 * `field_groups` is one JSON column holding two shapes: an agent *build*
 * writes `field_locators` + `reveal_steps`, and the studio writes `bindings` +
 * `steps` (`save_document` stores the v2 document's groups verbatim). Nothing
 * converts between them.
 *
 * This existed as `group.reveal_steps.length` inline, which is fine for a v1
 * recipe and a `TypeError` for every recipe authored in the studio -- so the
 * detail page white-screened on exactly the recipes the wizard produces. The
 * defaulting is the entire fix; it is a function so it can be tested without
 * rendering.
 */
export function readGroup(group: RecipeFieldGroup): ReadableGroup {
  return {
    locators: group.bindings ?? group.field_locators ?? null,
    steps: group.steps ?? group.reveal_steps ?? [],
    v2: group.bindings !== undefined,
  }
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
