import { Plus, X } from 'lucide-react'
import { Input } from '@/components/ui/input'
import { Button } from '@/components/ui/button'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { ASSERTION_KINDS, newAssertion, removeAt, replaceAt } from '@/lib/recipe/document'
import type { Assertion, AssertionKind } from '@/lib/recipe/types'

// Assertions are the cheapest defence against the failure that quietly
// poisons a dataset: a selector that drifts onto the wrong element but keeps
// producing well-typed values forever. The hints say what each one catches,
// because "range" on its own does not explain why anyone would add it.
const HINTS: Record<AssertionKind, string> = {
  not_empty: 'fails the field when it resolves to nothing',
  range: 'a price of 0 or 10^9 is a drifted selector, not a bargain',
  matches: 'shape check -- a SKU that stops looking like a SKU',
  in_set: 'closed vocabularies: currency, availability, size',
  length: 'catches a description that collapsed to one word',
  cross_source_agrees: 'compares two candidates of different kinds; disagreement is high-signal drift',
  not_equals_previous: 'flags a value frozen across runs -- often a cached or blocked page',
}

function AssertionFields({ assertion, onChange }: { assertion: Assertion; onChange: (a: Assertion) => void }) {
  const set = (patch: Partial<Assertion>) => onChange({ ...assertion, ...patch })
  const num = (v: string) => (v === '' ? null : Number(v))

  switch (assertion.kind) {
    case 'range':
    case 'length':
      return (
        <>
          <Input
            type="number"
            className="h-7 w-24 text-xs"
            placeholder="min"
            value={assertion.min ?? ''}
            onChange={(e) => set({ min: num(e.target.value) })}
          />
          <Input
            type="number"
            className="h-7 w-24 text-xs"
            placeholder="max"
            value={assertion.max ?? ''}
            onChange={(e) => set({ max: num(e.target.value) })}
          />
        </>
      )
    case 'matches':
      return (
        <Input
          className="h-7 flex-1 font-mono text-xs"
          placeholder="regex"
          value={assertion.regex ?? ''}
          onChange={(e) => set({ regex: e.target.value })}
        />
      )
    case 'in_set':
      return (
        <Input
          className="h-7 flex-1 font-mono text-xs"
          placeholder="comma-separated allowed values"
          value={(assertion.values ?? []).map(String).join(', ')}
          onChange={(e) => set({ values: e.target.value.split(',').map((s) => s.trim()).filter(Boolean) })}
        />
      )
    case 'cross_source_agrees':
      return (
        <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
          tolerance
          <Input
            type="number"
            step="0.01"
            className="h-7 w-20 text-xs"
            value={assertion.tolerance ?? 0}
            onChange={(e) => set({ tolerance: Number(e.target.value) })}
          />
        </label>
      )
    default:
      return null
  }
}

interface Props {
  assertions: Assertion[]
  onChange: (next: Assertion[]) => void
}

export function AssertionList({ assertions, onChange }: Props) {
  return (
    <div className="flex flex-col gap-1.5">
      {assertions.map((assertion, i) => (
        <div key={i} className="flex flex-col gap-0.5">
          <div className="flex items-center gap-1.5">
            <Select
              value={assertion.kind}
              onValueChange={(kind) => onChange(replaceAt(assertions, i, newAssertion(kind as AssertionKind)))}
            >
              <SelectTrigger className="h-7 w-44 text-xs">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {ASSERTION_KINDS.map((kind) => (
                  <SelectItem key={kind} value={kind}>
                    {kind}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <AssertionFields assertion={assertion} onChange={(next) => onChange(replaceAt(assertions, i, next))} />
            <Button size="icon" variant="ghost" className="size-6" onClick={() => onChange(removeAt(assertions, i))}>
              <X className="size-3" />
            </Button>
          </div>
          <span className="pl-1 text-[11px] text-muted-foreground">{HINTS[assertion.kind]}</span>
        </div>
      ))}
      <Button
        size="sm"
        variant="ghost"
        className="h-6 w-fit px-1.5 text-xs text-muted-foreground"
        onClick={() => onChange([...assertions, newAssertion()])}
      >
        <Plus className="size-3" />
        add assertion
      </Button>
    </div>
  )
}
