import { Plus, Trash2 } from 'lucide-react'
import { Input } from '@/components/ui/input'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Reorderable } from '@/components/ui/reorderable'
import { CandidateRow } from '@/components/app/studio/CandidateRow'
import { LocatorEditor } from '@/components/app/studio/LocatorEditor'
import {
  LOCATOR_KINDS,
  newCandidate,
  newGroup,
  newLocator,
  removeAt,
  reorderCandidates,
  replaceAt,
  updateGroup,
} from '@/lib/recipe/document'
import type { FieldGroup, LocatorKind, Recipe, RepeatSpec } from '@/lib/recipe/types'

interface Props {
  recipe: Recipe
  update: (fn: (r: Recipe) => Recipe) => void
  selectedField: string | null
  onSelectField: (name: string | null) => void
  luaEnabled: boolean
}

export function FieldsTab({ recipe, update, selectedField, onSelectField, luaEnabled }: Props) {
  return (
    <div className="flex flex-col gap-5">
      {recipe.field_groups.map((group) => (
        <GroupSection
          key={group.group_id}
          group={group}
          recipe={recipe}
          update={update}
          selectedField={selectedField}
          onSelectField={onSelectField}
          luaEnabled={luaEnabled}
        />
      ))}

      <Button
        size="sm"
        variant="outline"
        className="w-fit"
        onClick={() => update((r) => ({ ...r, field_groups: [...r.field_groups, newGroup(r.field_groups)] }))}
      >
        <Plus className="size-3.5" />
        add group
      </Button>
      <p className="text-[11px] text-muted-foreground">
        A group is a set of fields reachable from the same page state. Fields that need no interaction belong together
        in one group; a section behind a click needs its own, because its steps run for every field in it.
      </p>
    </div>
  )
}

function GroupSection({
  group,
  recipe,
  update,
  selectedField,
  onSelectField,
  luaEnabled,
}: {
  group: FieldGroup
  recipe: Recipe
  update: Props['update']
  selectedField: string | null
  onSelectField: (name: string | null) => void
  luaEnabled: boolean
}) {
  const patch = (p: Partial<FieldGroup>) => update((r) => updateGroup(r, group.group_id, p))

  return (
    <section className="rounded-lg border border-border">
      <div className="flex flex-wrap items-center gap-2 border-b border-border bg-muted/40 px-2 py-1.5">
        <Input
          className="h-7 w-48 font-mono text-xs"
          defaultValue={group.group_id}
          onBlur={(e) => {
            const next = e.target.value.trim()
            if (next && next !== group.group_id) patch({ group_id: next })
          }}
        />
        <Badge variant="outline">{group.field_names.length} fields</Badge>
        {(group.steps ?? []).length > 0 && (
          <Badge variant="outline">{(group.steps ?? []).length} steps</Badge>
        )}
        {group.repeat && <Badge variant="accent">repeat: {group.repeat.kind}</Badge>}
        <div className="ml-auto flex items-center gap-1.5 text-[11px] text-muted-foreground">
          <label className="flex items-center gap-1">
            min rows
            <Input
              type="number"
              className="h-6 w-16 text-xs"
              value={group.expect?.min_rows ?? ''}
              onChange={(e) =>
                patch({
                  expect: { ...group.expect, min_rows: e.target.value ? Number(e.target.value) : null },
                })
              }
            />
          </label>
          <Button
            size="icon"
            variant="ghost"
            className="size-6"
            title="Delete group"
            onClick={() =>
              update((r) => ({
                ...r,
                field_groups: r.field_groups.filter((g) => g.group_id !== group.group_id),
              }))
            }
          >
            <Trash2 className="size-3.5" />
          </Button>
        </div>
      </div>

      <RepeatEditor group={group} patch={patch} />

      <div className="flex flex-col gap-3 p-2">
        {group.field_names.length === 0 && (
          <span className="text-xs text-muted-foreground">
            No fields. Assign one from the schema pane on the left.
          </span>
        )}
        {group.field_names.map((name) => {
          const candidates = group.bindings[name] ?? []
          const setCandidates = (next: typeof candidates) =>
            patch({ bindings: { ...group.bindings, [name]: next } })
          const selected = selectedField === name

          return (
            <div key={name} className="flex flex-col gap-1.5">
              <button
                type="button"
                onClick={() => onSelectField(selected ? null : name)}
                className="flex w-fit items-center gap-2 text-left"
              >
                <span className="font-mono text-xs font-semibold">{name}</span>
                {recipe.fields[name]?.required && <Badge variant="outline">required</Badge>}
                {recipe.fields[name]?.description && (
                  <span className="truncate text-[11px] text-muted-foreground">
                    {recipe.fields[name].description}
                  </span>
                )}
              </button>

              {candidates.map((candidate, i) => (
                <Reorderable
                  key={i}
                  index={i}
                  count={candidates.length}
                  group={`cand:${group.group_id}:${name}`}
                  label={`candidate ${i + 1} of ${candidates.length} for ${name}`}
                  onMove={(from, to) => setCandidates(reorderCandidates(candidates, from, to))}
                >
                  <CandidateRow
                    candidate={candidate}
                    position={i + 1}
                    variants={recipe.variants ?? []}
                    luaEnabled={luaEnabled}
                    groupKey={`${group.group_id}:${name}:${i}`}
                    onChange={(next) => setCandidates(replaceAt(candidates, i, next))}
                    onRemove={() => setCandidates(removeAt(candidates, i))}
                  />
                </Reorderable>
              ))}

              <div className="flex items-center gap-1.5 pl-5">
                <Select
                  value=""
                  onValueChange={(kind) => setCandidates([...candidates, newCandidate(kind as LocatorKind)])}
                >
                  <SelectTrigger className="h-6 w-44 text-[11px] text-muted-foreground">
                    <SelectValue placeholder="+ add candidate" />
                  </SelectTrigger>
                  <SelectContent>
                    {LOCATOR_KINDS.map((kind) => (
                      <SelectItem key={kind} value={kind}>
                        {kind}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                {candidates.length === 0 && (
                  <span className="text-[11px] text-destructive">no candidates -- this field can never resolve</span>
                )}
              </div>
            </div>
          )
        })}
      </div>
    </section>
  )
}

function RepeatEditor({ group, patch }: { group: FieldGroup; patch: (p: Partial<FieldGroup>) => void }) {
  const repeat = group.repeat

  if (!repeat) {
    return (
      <div className="border-b border-border px-2 py-1.5">
        <Button
          size="sm"
          variant="ghost"
          className="h-6 px-1.5 text-xs text-muted-foreground"
          onClick={() =>
            patch({
              repeat: {
                kind: 'dom',
                max_iterations: 20,
                row_field: group.field_names[0] ?? '',
                option_locator: newLocator('ax_role'),
              },
            })
          }
        >
          <Plus className="size-3" />
          collect repeating rows
        </Button>
      </div>
    )
  }

  const set = (p: Partial<RepeatSpec>) => patch({ repeat: { ...repeat, ...p } })

  return (
    <div className="flex flex-col gap-2 border-b border-border bg-muted/20 px-2 py-2">
      <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
        <Select value={repeat.kind} onValueChange={(v) => set({ kind: v as RepeatSpec['kind'] })}>
          <SelectTrigger className="h-7 w-28 text-xs">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="dom">dom (click each)</SelectItem>
            <SelectItem value="json">json (iterate)</SelectItem>
          </SelectContent>
        </Select>
        <label className="flex items-center gap-1.5">
          row field
          <Select value={repeat.row_field} onValueChange={(v) => set({ row_field: v })}>
            <SelectTrigger className="h-7 w-40 text-xs">
              <SelectValue placeholder="pick a field" />
            </SelectTrigger>
            <SelectContent>
              {group.field_names.map((n) => (
                <SelectItem key={n} value={n}>
                  {n}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </label>
        <label className="flex items-center gap-1.5">
          max iterations
          <Input
            type="number"
            className="h-7 w-20 text-xs"
            value={repeat.max_iterations}
            onChange={(e) => set({ max_iterations: Number(e.target.value) })}
          />
        </label>
        <Button size="icon" variant="ghost" className="size-6" onClick={() => patch({ repeat: undefined })}>
          <Trash2 className="size-3.5" />
        </Button>
      </div>

      <div className="flex flex-col gap-1">
        <span className="text-[11px] font-medium text-muted-foreground">
          {repeat.kind === 'dom' ? 'Option locator (clicked once per row)' : 'Rows locator (the JSON array)'}
        </span>
        {repeat.kind === 'dom' ? (
          <LocatorEditor
            locator={repeat.option_locator ?? newLocator('ax_role')}
            onChange={(option_locator) => set({ option_locator })}
            actionTarget
          />
        ) : (
          <LocatorEditor
            locator={repeat.rows_locator ?? newLocator('json_ld')}
            onChange={(rows_locator) => set({ rows_locator })}
          />
        )}
      </div>

      {repeat.kind === 'dom' && (
        <p className="text-[11px] text-muted-foreground">
          A dom repeat sets the option index per iteration, so this must be a locator the driver can index --{' '}
          <code className="font-mono">ax_role</code> resolves through the fused tree and can. A css selector cannot,
          and would click the first option on every pass.
        </p>
      )}
      {repeat.max_iterations > 0 && (
        <p className="text-[11px] text-muted-foreground">
          Hitting {repeat.max_iterations} iterations marks the field truncated rather than reporting success. Set
          min rows above so a short run is caught, not just recorded.
        </p>
      )}
    </div>
  )
}
