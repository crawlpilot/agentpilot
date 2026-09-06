import { useState } from 'react'
import { ChevronDown, ChevronRight, Plus, Trash2 } from 'lucide-react'
import { Input } from '@/components/ui/input'
import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/label'
import { Badge } from '@/components/ui/badge'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { StringListField } from '@/components/app/StringListField'
import { TypeSpecEditor } from '@/components/app/studio/TypeSpecEditor'
import { AssertionList } from '@/components/app/studio/AssertionEditor'
import {
  addField,
  candidatesFor,
  describeTypeSpec,
  groupForField,
  moveFieldToGroup,
  removeField,
  renameField,
  updateField,
} from '@/lib/recipe/document'
import type { Recipe, UrlMatcher, UrlMatcherKind } from '@/lib/recipe/types'
import { cn } from '@/lib/utils'

interface Props {
  recipe: Recipe
  update: (fn: (r: Recipe) => Recipe) => void
  selectedField: string | null
  onSelectField: (name: string | null) => void
}

/**
 * The left pane: the caller's data contract.
 *
 * It comes first in the layout because it is the input everything else works
 * from -- the build agent reads these descriptions, and a field that is not
 * declared here cannot be collected no matter what the selectors say.
 */
export function SchemaPane({ recipe, update, selectedField, onSelectField }: Props) {
  const [newName, setNewName] = useState('')

  function submitNewField() {
    const name = newName.trim()
    if (!name) return
    update((r) => addField(r, name))
    onSelectField(name)
    setNewName('')
  }

  return (
    <div className="flex h-full flex-col overflow-y-auto">
      <div className="flex flex-col gap-3 border-b border-border p-3">
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="studio-name">Recipe name</Label>
          <Input
            id="studio-name"
            className="text-sm"
            placeholder="zara-product"
            value={recipe.name}
            onChange={(e) => update((r) => ({ ...r, name: e.target.value }))}
          />
        </div>

        <StringListField
          label="Sample URLs"
          rows={3}
          placeholder={'one per line -- three or more pages of the same type'}
          value={recipe.sample_urls}
          onChange={(sample_urls) => update((r) => ({ ...r, sample_urls }))}
        />
        <p className="-mt-1 text-[11px] text-muted-foreground">
          A selector verified on one page is a guess. Verified on three, it is an induced wrapper -- which is the
          difference between a recipe that survives the next page and one that only ever worked on the page it was
          written against.
        </p>

        <TargetEditor recipe={recipe} update={update} />
      </div>

      <div className="flex items-center justify-between gap-2 px-3 pt-3">
        <h2 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          Fields ({Object.keys(recipe.fields).length})
        </h2>
      </div>

      <div className="flex flex-col gap-1 p-3 pt-2">
        {Object.entries(recipe.fields).map(([name, spec]) => {
          const expanded = selectedField === name
          const group = groupForField(recipe, name)
          const candidates = candidatesFor(recipe, name)
          return (
            <div
              key={name}
              className={cn(
                'rounded-md border text-sm',
                expanded ? 'border-accent bg-accent-tint/40' : 'border-border',
              )}
            >
              <button
                type="button"
                className="flex w-full items-center gap-1.5 px-2 py-1.5 text-left"
                onClick={() => onSelectField(expanded ? null : name)}
              >
                {expanded ? <ChevronDown className="size-3.5 shrink-0" /> : <ChevronRight className="size-3.5 shrink-0" />}
                <span className="min-w-0 flex-1 truncate font-mono text-xs">{name}</span>
                {spec.required && <Badge variant="outline">req</Badge>}
                <span className="shrink-0 text-[11px] text-muted-foreground">{describeTypeSpec(spec.type)}</span>
                <Badge variant={candidates.length === 0 ? 'destructive' : 'default'} className="shrink-0">
                  {candidates.length}
                </Badge>
              </button>

              {expanded && (
                <div className="flex flex-col gap-2.5 border-t border-border/60 p-2">
                  <div className="flex flex-col gap-1">
                    <Label className="text-[11px]">Name</Label>
                    <Input
                      className="h-7 font-mono text-xs"
                      defaultValue={name}
                      onBlur={(e) => {
                        const next = e.target.value.trim()
                        if (next && next !== name) {
                          update((r) => renameField(r, name, next))
                          onSelectField(next)
                        }
                      }}
                    />
                  </div>

                  <div className="flex flex-col gap-1">
                    <Label className="text-[11px]">Description</Label>
                    <Input
                      className="h-7 text-xs"
                      placeholder="read by the build agent -- write it for a reader"
                      value={spec.description ?? ''}
                      onChange={(e) => update((r) => updateField(r, name, { ...spec, description: e.target.value }))}
                    />
                  </div>

                  <div className="flex flex-col gap-1">
                    <Label className="text-[11px]">Type</Label>
                    <TypeSpecEditor
                      type={spec.type}
                      onChange={(type) => update((r) => updateField(r, name, { ...spec, type }))}
                    />
                  </div>

                  <div className="flex flex-wrap items-center gap-3 text-xs text-muted-foreground">
                    <label className="flex items-center gap-1.5">
                      <input
                        type="checkbox"
                        checked={spec.required ?? false}
                        onChange={(e) => update((r) => updateField(r, name, { ...spec, required: e.target.checked }))}
                      />
                      required
                    </label>
                    <label className="flex items-center gap-1.5" title="Also emit the pre-transform value">
                      <input
                        type="checkbox"
                        checked={spec.emit_raw ?? false}
                        onChange={(e) => update((r) => updateField(r, name, { ...spec, emit_raw: e.target.checked }))}
                      />
                      emit raw
                    </label>
                  </div>

                  <div className="flex flex-col gap-1">
                    <Label className="text-[11px]">Collected by group</Label>
                    <Select
                      value={group?.group_id ?? ''}
                      onValueChange={(id) => update((r) => moveFieldToGroup(r, name, id))}
                    >
                      <SelectTrigger className="h-7 text-xs">
                        <SelectValue placeholder="not collected" />
                      </SelectTrigger>
                      <SelectContent>
                        {recipe.field_groups.map((g) => (
                          <SelectItem key={g.group_id} value={g.group_id}>
                            {g.group_id}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </div>

                  <div className="flex flex-col gap-1">
                    <Label className="text-[11px]">Assertions</Label>
                    <AssertionList
                      assertions={spec.assertions ?? []}
                      onChange={(assertions) => update((r) => updateField(r, name, { ...spec, assertions }))}
                    />
                  </div>

                  <Button
                    size="sm"
                    variant="ghost"
                    className="h-7 w-fit px-1.5 text-xs text-destructive"
                    onClick={() => {
                      update((r) => removeField(r, name))
                      onSelectField(null)
                    }}
                  >
                    <Trash2 className="size-3" />
                    delete field
                  </Button>
                </div>
              )}
            </div>
          )
        })}

        <div className="flex items-center gap-1.5 pt-1">
          <Input
            className="h-7 font-mono text-xs"
            placeholder="new field name"
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') submitNewField()
            }}
          />
          <Button size="icon" variant="outline" className="size-7" onClick={submitNewField} aria-label="Add field">
            <Plus className="size-3.5" />
          </Button>
        </div>
      </div>
    </div>
  )
}

const MATCHER_KINDS: UrlMatcherKind[] = ['glob', 'regex', 'host']

function TargetEditor({ recipe, update }: { recipe: Recipe; update: Props['update'] }) {
  function setMatch(match: UrlMatcher[]) {
    update((r) => ({ ...r, target: { ...r.target, match } }))
  }

  return (
    <div className="flex flex-col gap-1.5">
      <Label>URL guard</Label>
      <p className="text-[11px] text-muted-foreground">
        Not a navigate target -- the URL arrives with each run. This decides which runs this recipe will accept.
      </p>
      {recipe.target.match.map((matcher, i) => (
        <div key={i} className="flex items-center gap-1.5">
          <Select
            value={matcher.kind}
            onValueChange={(kind) =>
              setMatch(recipe.target.match.map((m, j) => (j === i ? { ...m, kind: kind as UrlMatcherKind } : m)))
            }
          >
            <SelectTrigger className="h-7 w-24 text-xs">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {MATCHER_KINDS.map((kind) => (
                <SelectItem key={kind} value={kind}>
                  {kind}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Input
            className="h-7 flex-1 font-mono text-xs"
            placeholder={matcher.kind === 'host' ? 'www.zara.com' : 'https://www.zara.com/*/*-p*.html'}
            value={matcher.pattern}
            onChange={(e) =>
              setMatch(recipe.target.match.map((m, j) => (j === i ? { ...m, pattern: e.target.value } : m)))
            }
          />
          <Button
            size="icon"
            variant="ghost"
            className="size-6"
            onClick={() => setMatch(recipe.target.match.filter((_, j) => j !== i))}
          >
            <Trash2 className="size-3" />
          </Button>
        </div>
      ))}
      <Button
        size="sm"
        variant="ghost"
        className="h-6 w-fit px-1.5 text-xs text-muted-foreground"
        onClick={() => setMatch([...recipe.target.match, { kind: 'glob', pattern: '' }])}
      >
        <Plus className="size-3" />
        add matcher
      </Button>
    </div>
  )
}
