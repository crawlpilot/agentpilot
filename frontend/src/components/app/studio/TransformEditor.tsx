import { Plus, X } from 'lucide-react'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Button } from '@/components/ui/button'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Reorderable } from '@/components/ui/reorderable'
import { TRANSFORM_OPS, moveItem, newTransform, removeAt, replaceAt } from '@/lib/recipe/document'
import type { Transform, TransformOp, ValueType } from '@/lib/recipe/types'

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

function TransformFields({ transform, onChange }: { transform: Transform; onChange: (t: Transform) => void }) {
  const set = (patch: Partial<Transform>) => onChange({ ...transform, ...patch })
  const num = (v: string) => (v === '' ? undefined : Number(v))

  switch (transform.op) {
    case 'regex_extract':
      return (
        <>
          <Input
            className="h-7 flex-1 font-mono text-xs"
            placeholder="pattern"
            value={transform.pattern ?? ''}
            onChange={(e) => set({ pattern: e.target.value })}
          />
          <Input
            type="number"
            className="h-7 w-16 text-xs"
            title="capture group"
            value={transform.group ?? 0}
            onChange={(e) => set({ group: num(e.target.value) })}
          />
          <Input
            className="h-7 w-16 font-mono text-xs"
            placeholder="flags"
            value={transform.flags ?? ''}
            onChange={(e) => set({ flags: e.target.value || undefined })}
          />
        </>
      )
    case 'regex_replace':
      return (
        <>
          <Input
            className="h-7 flex-1 font-mono text-xs"
            placeholder="pattern"
            value={transform.pattern ?? ''}
            onChange={(e) => set({ pattern: e.target.value })}
          />
          <Input
            className="h-7 flex-1 font-mono text-xs"
            placeholder="replacement"
            value={transform.repl ?? ''}
            onChange={(e) => set({ repl: e.target.value })}
          />
          <Input
            className="h-7 w-16 font-mono text-xs"
            placeholder="flags"
            value={transform.flags ?? ''}
            onChange={(e) => set({ flags: e.target.value || undefined })}
          />
        </>
      )
    case 'case':
      return (
        <Select value={transform.mode ?? 'lower'} onValueChange={(v) => set({ mode: v as Transform['mode'] })}>
          <SelectTrigger className="h-7 w-28 text-xs">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="lower">lower</SelectItem>
            <SelectItem value="upper">upper</SelectItem>
            <SelectItem value="title">title</SelectItem>
          </SelectContent>
        </Select>
      )
    case 'split':
      return (
        <>
          <Input
            className="h-7 flex-1 font-mono text-xs"
            placeholder="separator"
            value={transform.sep ?? ''}
            onChange={(e) => set({ sep: e.target.value })}
          />
          <Input
            type="number"
            className="h-7 w-20 text-xs"
            title="max splits (-1 for all)"
            value={transform.limit ?? ''}
            onChange={(e) => set({ limit: num(e.target.value) })}
          />
        </>
      )
    case 'join':
      return (
        <Input
          className="h-7 flex-1 font-mono text-xs"
          placeholder="separator"
          value={transform.sep ?? ''}
          onChange={(e) => set({ sep: e.target.value })}
        />
      )
    case 'slice':
      return (
        <>
          <Input
            type="number"
            className="h-7 w-20 text-xs"
            placeholder="start"
            value={transform.start ?? ''}
            onChange={(e) => set({ start: e.target.value === '' ? null : Number(e.target.value) })}
          />
          <Input
            type="number"
            className="h-7 w-20 text-xs"
            placeholder="end"
            value={transform.end ?? ''}
            onChange={(e) => set({ end: e.target.value === '' ? null : Number(e.target.value) })}
          />
        </>
      )
    case 'index':
      return (
        <Input
          type="number"
          className="h-7 w-20 text-xs"
          value={transform.i ?? 0}
          onChange={(e) => set({ i: num(e.target.value) })}
        />
      )
    case 'template':
      return (
        <Input
          className="h-7 flex-1 font-mono text-xs"
          placeholder="{{v}} -- and {{meta.sku}} from the run's metadata"
          value={transform.format ?? ''}
          onChange={(e) => set({ format: e.target.value })}
        />
      )
    case 'json_path':
      return (
        <>
          <Input
            className="h-7 flex-1 font-mono text-xs"
            placeholder="path"
            value={transform.path ?? ''}
            onChange={(e) => set({ path: e.target.value })}
          />
          <Select
            value={transform.path_lang ?? 'simple'}
            onValueChange={(v) => set({ path_lang: v as Transform['path_lang'] })}
          >
            <SelectTrigger className="h-7 w-28 text-xs">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="simple">simple</SelectItem>
              <SelectItem value="jmespath">jmespath</SelectItem>
            </SelectContent>
          </Select>
        </>
      )
    case 'html_select':
      return (
        <>
          <Input
            className="h-7 flex-1 font-mono text-xs"
            placeholder="css selector, applied to the HTML string"
            value={transform.selector ?? ''}
            onChange={(e) => set({ selector: e.target.value })}
          />
          <Input
            className="h-7 w-24 font-mono text-xs"
            placeholder="attribute"
            value={transform.attribute ?? ''}
            onChange={(e) => set({ attribute: e.target.value || undefined })}
          />
          <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
            <input type="checkbox" checked={transform.all ?? false} onChange={(e) => set({ all: e.target.checked })} />
            all
          </label>
        </>
      )
    case 'to_object':
    case 'to_pairs':
      return (
        <>
          <Input
            className="h-7 w-32 font-mono text-xs"
            placeholder={transform.op === 'to_object' ? 'key field (name)' : 'key name (key)'}
            value={transform.key ?? ''}
            onChange={(e) => set({ key: e.target.value || undefined })}
          />
          <Input
            className="h-7 w-32 font-mono text-xs"
            placeholder="value field (value)"
            value={typeof transform.value === 'string' ? transform.value : ''}
            onChange={(e) => set({ value: e.target.value || undefined })}
          />
        </>
      )
    case 'map_lookup':
      return (
        <Input
          className="h-7 flex-1 font-mono text-xs"
          placeholder='{"In stock": true, "Sold out": false}'
          value={transform.table ? JSON.stringify(transform.table) : ''}
          onChange={(e) => {
            try {
              set({ table: e.target.value ? (JSON.parse(e.target.value) as Record<string, unknown>) : undefined })
            } catch {
              // Keep the last valid table while the author is mid-keystroke;
              // the lint reports an unusable map, the editor does not fight them.
            }
          }}
        />
      )
    case 'cast':
      return (
        <Select value={transform.to ?? 'string'} onValueChange={(v) => set({ to: v as ValueType })}>
          <SelectTrigger className="h-7 w-32 text-xs">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {VALUE_TYPES.map((t) => (
              <SelectItem key={t} value={t}>
                {t}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      )
    case 'default':
      return (
        <Input
          className="h-7 flex-1 font-mono text-xs"
          placeholder="fallback value (JSON)"
          value={transform.value === undefined ? '' : JSON.stringify(transform.value)}
          onChange={(e) => {
            try {
              set({ value: e.target.value ? JSON.parse(e.target.value) : undefined })
            } catch {
              set({ value: e.target.value })
            }
          }}
        />
      )
    case 'lua':
      return (
        <Textarea
          rows={4}
          className="flex-1 font-mono text-xs"
          placeholder={'return function(v, ctx)\n  return v\nend'}
          value={transform.source ?? ''}
          onChange={(e) => set({ source: e.target.value })}
        />
      )
    default:
      return <span className="text-xs text-muted-foreground">no parameters</span>
  }
}

interface Props {
  transforms: Transform[]
  onChange: (next: Transform[]) => void
  group: string
  /** Shown above the list -- the pipeline runs top to bottom. */
  title?: string
  luaEnabled?: boolean
}

export function TransformList({ transforms, onChange, group, title, luaEnabled = true }: Props) {
  const ops = luaEnabled ? TRANSFORM_OPS : TRANSFORM_OPS.filter((op) => op !== 'lua')
  return (
    <div className="flex flex-col gap-1.5">
      {title && <div className="text-xs font-medium text-muted-foreground">{title}</div>}
      {transforms.map((transform, i) => (
        <Reorderable
          key={i}
          index={i}
          count={transforms.length}
          group={group}
          label={`${transform.op}, step ${i + 1} of ${transforms.length}`}
          onMove={(from, to) => onChange(moveItem(transforms, from, to))}
        >
          <div className="flex flex-wrap items-start gap-1.5">
            <Select
              value={transform.op}
              onValueChange={(op) => onChange(replaceAt(transforms, i, newTransform(op as TransformOp)))}
            >
              <SelectTrigger className="h-7 w-36 text-xs">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {ops.map((op) => (
                  <SelectItem key={op} value={op}>
                    {op}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <TransformFields transform={transform} onChange={(next) => onChange(replaceAt(transforms, i, next))} />
            <Button size="icon" variant="ghost" className="size-6" onClick={() => onChange(removeAt(transforms, i))}>
              <X className="size-3" />
            </Button>
          </div>
        </Reorderable>
      ))}
      <Button
        size="sm"
        variant="ghost"
        className="h-6 w-fit px-1.5 text-xs text-muted-foreground"
        onClick={() => onChange([...transforms, newTransform()])}
      >
        <Plus className="size-3" />
        add transform
      </Button>
    </div>
  )
}
