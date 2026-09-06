import { Plus, X } from 'lucide-react'
import { Input } from '@/components/ui/input'
import { Button } from '@/components/ui/button'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { PREDICATE_KINDS, newPredicate, removeAt, replaceAt } from '@/lib/recipe/document'
import type { Predicate, PredicateKind } from '@/lib/recipe/types'

function PredicateFields({ predicate, onChange }: { predicate: Predicate; onChange: (p: Predicate) => void }) {
  const set = (patch: Partial<Predicate>) => onChange({ ...predicate, ...patch })
  switch (predicate.kind) {
    case 'selector_present':
    case 'selector_absent':
    case 'visible':
      return (
        <Input
          className="h-7 flex-1 font-mono text-xs"
          placeholder="css selector"
          value={predicate.selector ?? ''}
          onChange={(e) => set({ selector: e.target.value })}
        />
      )
    case 'text_present':
      return (
        <Input
          className="h-7 flex-1 text-xs"
          placeholder="text on the page"
          value={predicate.text ?? ''}
          onChange={(e) => set({ text: e.target.value })}
        />
      )
    case 'url_matches':
      return (
        <Input
          className="h-7 flex-1 font-mono text-xs"
          placeholder="regex against the current URL"
          value={predicate.url ?? ''}
          onChange={(e) => set({ url: e.target.value })}
        />
      )
    case 'json_path_present':
      return (
        <>
          <Select value={predicate.source ?? 'json_ld'} onValueChange={(v) => set({ source: v as Predicate['source'] })}>
            <SelectTrigger className="h-7 w-28 text-xs">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="json_ld">json_ld</SelectItem>
              <SelectItem value="hydration">hydration</SelectItem>
              <SelectItem value="meta">meta</SelectItem>
            </SelectContent>
          </Select>
          <Input
            className="h-7 flex-1 font-mono text-xs"
            placeholder="path"
            value={predicate.path ?? ''}
            onChange={(e) => set({ path: e.target.value })}
          />
        </>
      )
    case 'count_at_least':
      return (
        <>
          <Input
            className="h-7 flex-1 font-mono text-xs"
            placeholder="css selector"
            value={predicate.selector ?? ''}
            onChange={(e) => set({ selector: e.target.value })}
          />
          <Input
            type="number"
            className="h-7 w-20 text-xs"
            value={predicate.n ?? 1}
            onChange={(e) => set({ n: Number(e.target.value) })}
          />
        </>
      )
    case 'meta_equals':
      return (
        <>
          <Input
            className="h-7 w-40 font-mono text-xs"
            placeholder="metadata key"
            value={predicate.key ?? ''}
            onChange={(e) => set({ key: e.target.value })}
          />
          <Input
            className="h-7 flex-1 font-mono text-xs"
            placeholder="value"
            value={predicate.value ?? ''}
            onChange={(e) => set({ value: e.target.value })}
          />
        </>
      )
  }
}

interface ListProps {
  predicates: Predicate[]
  onChange: (next: Predicate[]) => void
  label: string
  /** Explains what an unsatisfied predicate does here -- it differs per use. */
  hint?: string
}

export function PredicateList({ predicates, onChange, label, hint }: ListProps) {
  return (
    <div className="flex flex-col gap-1.5">
      {predicates.map((predicate, i) => (
        <div key={i} className="flex items-center gap-1.5">
          <Select
            value={predicate.kind}
            onValueChange={(kind) => onChange(replaceAt(predicates, i, newPredicate(kind as PredicateKind)))}
          >
            <SelectTrigger className="h-7 w-40 text-xs">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {PREDICATE_KINDS.map((kind) => (
                <SelectItem key={kind} value={kind}>
                  {kind}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <PredicateFields predicate={predicate} onChange={(next) => onChange(replaceAt(predicates, i, next))} />
          <Button size="icon" variant="ghost" className="size-6" onClick={() => onChange(removeAt(predicates, i))}>
            <X className="size-3" />
          </Button>
        </div>
      ))}
      <div className="flex items-center gap-2">
        <Button
          size="sm"
          variant="ghost"
          className="h-6 px-1.5 text-xs text-muted-foreground"
          onClick={() => onChange([...predicates, newPredicate()])}
        >
          <Plus className="size-3" />
          {label}
        </Button>
        {hint && predicates.length === 0 && <span className="text-xs text-muted-foreground">{hint}</span>}
      </div>
    </div>
  )
}
