import { Plus } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { PlaygroundActionRow } from '@/components/app/PlaygroundActionRow'
import { ACTION_TYPES, type ActionIn } from '@/lib/api/types'

function defaultActionFor(type: ActionIn['type']): ActionIn {
  switch (type) {
    case 'navigate':
      return { type, url: '' }
    case 'go_back':
      return { type }
    case 'snapshot':
      return { type }
    case 'extract':
      return { type, format: 'markdown' }
    case 'screenshot':
      return { type }
    case 'wait':
      return { type, ms: 1000 }
    case 'execute_js':
      return { type, script: '' }
    case 'click':
      return { type, ref: '' }
    case 'fill':
      return { type, ref: '', text: '' }
    case 'select_option':
      return { type, ref: '', values: [] }
    case 'hover':
      return { type, ref: '' }
    case 'press':
      return { type, key: 'Enter' }
    case 'scroll':
      return { type, direction: 'down' }
  }
}

export function PlaygroundActionBuilder({
  actions,
  onChange,
  onPickRefForIndex,
}: {
  actions: ActionIn[]
  onChange: (actions: ActionIn[]) => void
  onPickRefForIndex: (index: number) => void
}) {
  function addAction(type: ActionIn['type']) {
    onChange([...actions, defaultActionFor(type)])
  }
  function updateAt(index: number, next: ActionIn) {
    onChange(actions.map((a, i) => (i === index ? next : a)))
  }
  function removeAt(index: number) {
    onChange(actions.filter((_, i) => i !== index))
  }
  function move(index: number, delta: number) {
    const target = index + delta
    if (target < 0 || target >= actions.length) return
    const next = [...actions]
    ;[next[index], next[target]] = [next[target], next[index]]
    onChange(next)
  }

  return (
    <div className="flex flex-col gap-2">
      {actions.map((action, i) => (
        <PlaygroundActionRow
          key={i}
          action={action}
          onChange={(next) => updateAt(i, next)}
          onRemove={() => removeAt(i)}
          onMoveUp={() => move(i, -1)}
          onMoveDown={() => move(i, 1)}
          onPickRef={() => onPickRefForIndex(i)}
        />
      ))}
      <Select onValueChange={(v) => addAction(v as ActionIn['type'])}>
        <SelectTrigger className="w-48">
          <Plus className="size-3.5" />
          <SelectValue placeholder="Add action" />
        </SelectTrigger>
        <SelectContent>
          {ACTION_TYPES.map((t) => (
            <SelectItem key={t} value={t}>
              {t}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  )
}
