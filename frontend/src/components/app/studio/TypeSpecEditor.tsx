import { Plus, X } from 'lucide-react'
import { Input } from '@/components/ui/input'
import { Button } from '@/components/ui/button'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import type { TypeKind, TypeSpec, ValueType } from '@/lib/recipe/types'

const VALUE_TYPES: ValueType[] = [
  'string',
  'text',
  'number',
  'float',
  'integer',
  'price',
  'boolean',
  'url',
  'date',
  'datetime',
  'json',
]

const KINDS: { kind: TypeKind; label: string }[] = [
  { kind: 'scalar', label: 'scalar' },
  { kind: 'list', label: 'list' },
  { kind: 'object', label: 'object / map' },
  { kind: 'table', label: 'table (rows)' },
]

function defaultFor(kind: TypeKind): TypeSpec {
  switch (kind) {
    case 'scalar':
      return { kind, value_type: 'string' }
    case 'list':
      return { kind, items: { kind: 'scalar', value_type: 'string' } }
    case 'object':
      return { kind, properties: {} }
    case 'table':
      return { kind, columns: {} }
  }
}

interface Props {
  type: TypeSpec
  onChange: (next: TypeSpec) => void
  depth?: number
}

export function TypeSpecEditor({ type, onChange, depth = 0 }: Props) {
  const members = type.kind === 'object' ? type.properties : type.kind === 'table' ? type.columns : undefined
  const memberKey = type.kind === 'object' ? 'properties' : 'columns'

  function setMembers(next: Record<string, TypeSpec>) {
    onChange({ ...type, [memberKey]: next })
  }

  function renameMember(from: string, to: string) {
    if (!to || from === to || members?.[to]) return
    const next: Record<string, TypeSpec> = {}
    for (const [key, spec] of Object.entries(members ?? {})) next[key === from ? to : key] = spec
    setMembers(next)
  }

  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex flex-wrap items-center gap-1.5">
        <Select value={type.kind} onValueChange={(k) => onChange(defaultFor(k as TypeKind))}>
          <SelectTrigger className="h-7 w-36 text-xs">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {KINDS.map(({ kind, label }) => (
              <SelectItem key={kind} value={kind}>
                {label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>

        {type.kind === 'scalar' && (
          <Select
            value={type.value_type ?? 'string'}
            onValueChange={(v) => onChange({ ...type, value_type: v as ValueType })}
          >
            <SelectTrigger className="h-7 w-32 text-xs">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {VALUE_TYPES.map((v) => (
                <SelectItem key={v} value={v}>
                  {v}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        )}

        {type.kind === 'list' && depth < 2 && (
          <span className="text-xs text-muted-foreground">of</span>
        )}
      </div>

      {type.kind === 'list' && depth < 2 && (
        <div className="border-l border-border pl-2">
          <TypeSpecEditor
            type={type.items ?? { kind: 'scalar', value_type: 'string' }}
            onChange={(items) => onChange({ ...type, items })}
            depth={depth + 1}
          />
        </div>
      )}

      {(type.kind === 'object' || type.kind === 'table') && (
        <div className="flex flex-col gap-1 border-l border-border pl-2">
          {Object.keys(members ?? {}).length === 0 && type.kind === 'object' && (
            // A spec sheet is exactly this: keys nobody can enumerate ahead of
            // time. Saying so beats leaving an empty list looking unfinished.
            <span className="text-xs text-muted-foreground">
              open map -- keys come from the page
            </span>
          )}
          {Object.entries(members ?? {}).map(([key, spec]) => (
            <div key={key} className="flex items-start gap-1.5">
              <Input
                className="h-7 w-36 font-mono text-xs"
                defaultValue={key}
                onBlur={(e) => renameMember(key, e.target.value.trim())}
              />
              <div className="min-w-0 flex-1">
                <TypeSpecEditor
                  type={spec}
                  onChange={(next) => setMembers({ ...members, [key]: next })}
                  depth={depth + 1}
                />
              </div>
              <Button
                size="icon"
                variant="ghost"
                className="size-6"
                onClick={() => {
                  const next = { ...members }
                  delete next[key]
                  setMembers(next)
                }}
              >
                <X className="size-3" />
              </Button>
            </div>
          ))}
          <Button
            size="sm"
            variant="ghost"
            className="h-6 w-fit px-1.5 text-xs text-muted-foreground"
            onClick={() => {
              let name = type.kind === 'table' ? 'column' : 'key'
              let n = 1
              while (members?.[`${name}_${n}`]) n += 1
              name = `${name}_${n}`
              setMembers({ ...members, [name]: { kind: 'scalar', value_type: 'string' } })
            }}
          >
            <Plus className="size-3" />
            {type.kind === 'table' ? 'add column' : 'declare a key'}
          </Button>
        </div>
      )}
    </div>
  )
}
