import { Plus, Trash2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Reorderable } from '@/components/ui/reorderable'
import { moveItem } from '@/lib/recipe/document'
import type { Transform, TransformOp, ValueType } from '@/lib/recipe/types'

interface Props {
  transforms: Transform[]
  onChange: (transforms: Transform[]) => void
  group: string
  /** The value the picker read, so the effect of a rule is visible. */
  sample?: string
}

/**
 * The cleanup rules offered here, and why this is not all 25 transform ops.
 *
 * These six answer the question an author actually has in front of a page:
 * "the page says `£1,299.00 incl. VAT` and I want `1299.00`". The rest of the
 * vocabulary -- `to_object`, `map_lookup`, `json_path`, `lua` -- solves
 * problems you discover later, from a run, not while picking; those stay in
 * the advanced editor where there is room to explain them.
 */
const OPS: { op: TransformOp; label: string; hint: string }[] = [
  { op: 'trim', label: 'Trim whitespace', hint: 'Leading and trailing spaces.' },
  { op: 'collapse_ws', label: 'Collapse whitespace', hint: 'Runs of spaces and newlines become one space.' },
  { op: 'regex_extract', label: 'Extract by pattern', hint: 'Keep only what the pattern captures.' },
  { op: 'regex_replace', label: 'Replace by pattern', hint: 'Rewrite every match.' },
  { op: 'strip_html', label: 'Strip HTML', hint: 'Remove tags, keep the text.' },
  { op: 'cast', label: 'Convert type', hint: 'Parse the cleaned string into a number, price or date.' },
]

const CAST_TO: ValueType[] = ['string', 'number', 'integer', 'float', 'price', 'boolean', 'url', 'date', 'datetime']

/**
 * Apply the rules to a sample locally, so the author sees the result.
 *
 * Deliberately an *approximation*, and labelled as one in the UI. The real
 * transforms run server-side in `recipe/v2/transform.py`; Python's `re` and
 * JavaScript's `RegExp` agree on the patterns anyone writes here but not on
 * every construct, and `cast` does locale-aware price parsing this cannot
 * reproduce. Showing the shape of the answer is useful; claiming it is the
 * answer would be the same lie the preview avoids elsewhere.
 */
export function applyLocally(sample: string, transforms: Transform[]): string {
  let value = sample
  for (const t of transforms) {
    try {
      if (t.op === 'trim') value = value.trim()
      else if (t.op === 'collapse_ws') value = value.replace(/\s+/g, ' ').trim()
      else if (t.op === 'strip_html') value = value.replace(/<[^>]*>/g, '')
      else if (t.op === 'regex_extract' && t.pattern) {
        const m = new RegExp(t.pattern, t.flags).exec(value)
        value = m ? (m[t.group ?? 1] ?? m[0] ?? '') : ''
      } else if (t.op === 'regex_replace' && t.pattern) {
        value = value.replace(new RegExp(t.pattern, t.flags ?? 'g'), t.repl ?? '')
      } else if (t.op === 'cast') {
        // Only the shape, not the server's parser -- see the note above.
        if (t.to === 'number' || t.to === 'float' || t.to === 'integer' || t.to === 'price') {
          const n = Number(value.replace(/[^\d.-]/g, ''))
          value = Number.isFinite(n) ? String(n) : ''
        }
      }
    } catch {
      // An unfinished pattern is the normal state while typing.
      return value
    }
  }
  return value
}

export function CleanupEditor({ transforms, onChange, group, sample }: Props) {
  function patch(index: number, next: Partial<Transform>) {
    onChange(transforms.map((t, i) => (i === index ? { ...t, ...next } : t)))
  }

  const cleaned = sample ? applyLocally(sample, transforms) : null

  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex items-center gap-1.5">
        <span className="text-[10px] uppercase tracking-wide text-muted-foreground">Cleanup</span>
        <Select value="" onValueChange={(op) => onChange([...transforms, { op: op as TransformOp }])}>
          <SelectTrigger className="ml-auto h-6 w-32 text-[11px]">
            <span className="flex items-center gap-1">
              <Plus className="size-3" />
              Add rule
            </span>
          </SelectTrigger>
          <SelectContent>
            {OPS.map((o) => (
              <SelectItem key={o.op} value={o.op} title={o.hint}>
                {o.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      {transforms.length === 0 ? (
        <p className="text-[10px] text-muted-foreground">
          None. The value is stored exactly as the page gives it.
        </p>
      ) : (
        <div>
          {transforms.map((t, index) => {
            const meta = OPS.find((o) => o.op === t.op)
            return (
              <Reorderable
                key={index}
                index={index}
                count={transforms.length}
                group={group}
                label={`rule ${index + 1} of ${transforms.length}`}
                onMove={(from, to) => onChange(moveItem(transforms, from, to))}
              >
                <div className="flex min-w-0 flex-col gap-1 py-1">
                  <div className="flex min-w-0 items-center gap-1.5">
                    <span className="shrink-0 text-[11px]">{meta?.label ?? t.op}</span>
                    <Button
                      size="sm"
                      variant="ghost"
                      className="ml-auto h-5 shrink-0 px-1"
                      aria-label={`Remove rule ${index + 1}`}
                      onClick={() => onChange(transforms.filter((_, i) => i !== index))}
                    >
                      <Trash2 className="size-3" />
                    </Button>
                  </div>

                  {(t.op === 'regex_extract' || t.op === 'regex_replace') && (
                    <div className="flex items-center gap-1">
                      <Input
                        className="h-6 flex-1 font-mono text-[11px]"
                        placeholder={t.op === 'regex_extract' ? '([\\d.,]+)' : ','}
                        value={t.pattern ?? ''}
                        onChange={(e) => patch(index, { pattern: e.target.value })}
                        aria-label="Pattern"
                      />
                      {t.op === 'regex_replace' ? (
                        <Input
                          className="h-6 w-20 font-mono text-[11px]"
                          placeholder="with"
                          value={t.repl ?? ''}
                          onChange={(e) => patch(index, { repl: e.target.value })}
                          aria-label="Replacement"
                        />
                      ) : (
                        <Input
                          className="h-6 w-16 text-[11px]"
                          type="number"
                          min={0}
                          placeholder="group"
                          value={t.group ?? 1}
                          onChange={(e) => patch(index, { group: Number(e.target.value) || 0 })}
                          aria-label="Capture group"
                        />
                      )}
                    </div>
                  )}

                  {t.op === 'cast' && (
                    <Select value={t.to ?? 'number'} onValueChange={(v) => patch(index, { to: v as ValueType })}>
                      <SelectTrigger className="h-6 w-28 text-[11px]">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        {CAST_TO.map((v) => (
                          <SelectItem key={v} value={v}>to {v}</SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  )}
                </div>
              </Reorderable>
            )
          })}
        </div>
      )}

      {sample && transforms.length > 0 && (
        <div className="rounded border border-border bg-muted/40 px-1.5 py-1">
          <p className="truncate font-mono text-[10px] text-muted-foreground" title={sample}>
            {sample}
          </p>
          <p className="truncate font-mono text-[10px]" title={cleaned ?? ''}>
            &rarr; {cleaned === '' ? <span className="text-destructive">(empty)</span> : cleaned}
          </p>
          <p className="pt-0.5 text-[10px] text-muted-foreground">
            Approximate &mdash; the real rules run server-side.
          </p>
        </div>
      )}
    </div>
  )
}
