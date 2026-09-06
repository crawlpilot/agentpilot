import { Plus } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Reorderable } from '@/components/ui/reorderable'
import { StepRow } from '@/components/app/studio/StepRow'
import { moveItem, newStep, removeAt, replaceAt, updateGroup } from '@/lib/recipe/document'
import type { Recipe, Step } from '@/lib/recipe/types'

interface Props {
  recipe: Recipe
  update: (fn: (r: Recipe) => Recipe) => void
}

function StepList({
  steps,
  onChange,
  group,
  allowSkipGroup,
}: {
  steps: Step[]
  onChange: (next: Step[]) => void
  group: string
  allowSkipGroup: boolean
}) {
  return (
    <div className="flex flex-col gap-1.5">
      {steps.map((step, i) => (
        <Reorderable
          key={i}
          index={i}
          count={steps.length}
          group={group}
          label={`${step.op}, step ${i + 1} of ${steps.length}`}
          onMove={(from, to) => onChange(moveItem(steps, from, to))}
        >
          <StepRow
            step={step}
            allowSkipGroup={allowSkipGroup}
            onChange={(next) => onChange(replaceAt(steps, i, next))}
            onRemove={() => onChange(removeAt(steps, i))}
          />
        </Reorderable>
      ))}
      <Button
        size="sm"
        variant="ghost"
        className="h-7 w-fit px-1.5 text-xs text-muted-foreground"
        onClick={() => onChange([...steps, newStep()])}
      >
        <Plus className="size-3.5" />
        add step
      </Button>
    </div>
  )
}

export function StepsTab({ recipe, update }: Props) {
  return (
    <div className="flex flex-col gap-5">
      <section className="flex flex-col gap-2">
        <div className="flex items-baseline gap-2">
          <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Global setup</h3>
          <span className="text-[11px] text-muted-foreground">
            runs once per group, after navigation -- consent walls, region interstitials, login
          </span>
        </div>
        <StepList
          steps={recipe.global_setup ?? []}
          group="global_setup"
          allowSkipGroup={false}
          onChange={(global_setup) => update((r) => ({ ...r, global_setup }))}
        />
        <p className="text-[11px] text-muted-foreground">
          A consent dialog that is usually absent should be <code className="font-mono">on_error: continue</code>, not{' '}
          <code className="font-mono">fail</code> -- otherwise the run dies on the pages that behaved.
        </p>
      </section>

      {recipe.field_groups.map((group) => (
        <section key={group.group_id} className="flex flex-col gap-2">
          <div className="flex items-baseline gap-2">
            <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">{group.group_id}</h3>
            <Badge variant="outline">{group.field_names.length} fields</Badge>
            {(group.steps ?? []).length === 0 && (
              <span className="text-[11px] text-muted-foreground">
                no interaction -- reads straight from the loaded page
              </span>
            )}
          </div>
          <StepList
            steps={group.steps ?? []}
            group={`steps:${group.group_id}`}
            allowSkipGroup
            onChange={(steps) => update((r) => updateGroup(r, group.group_id, { steps }))}
          />
        </section>
      ))}

      <p className="border-t border-border pt-3 text-[11px] text-muted-foreground">
        Every group re-navigates before its steps run, so a group's steps start from a clean page. That is deliberate --
        it is what stops one group's dismissed dialog from breaking the next -- and it is also why an extra group costs
        a page load.
      </p>
    </div>
  )
}
