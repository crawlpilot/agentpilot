import { ArrowDown, ArrowUp, Crosshair, Trash2 } from 'lucide-react'
import { Input } from '@/components/ui/input'
import { Button } from '@/components/ui/button'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import type { ActionIn } from '@/lib/api/types'

interface Props {
  action: ActionIn
  onChange: (next: ActionIn) => void
  onRemove: () => void
  onMoveUp: () => void
  onMoveDown: () => void
  onPickRef: () => void
}

const REF_ACTION_TYPES: ActionIn['type'][] = ['click', 'fill', 'hover', 'select_option', 'scroll']

export function PlaygroundActionRow({ action, onChange, onRemove, onMoveUp, onMoveDown, onPickRef }: Props) {
  const needsRef = REF_ACTION_TYPES.includes(action.type)

  function fields() {
    switch (action.type) {
      case 'navigate':
        return (
          <Input
            placeholder="https://example.com"
            value={action.url}
            onChange={(e) => onChange({ ...action, url: e.target.value })}
          />
        )
      case 'go_back':
        return <span className="text-xs text-muted-foreground">no parameters</span>
      case 'snapshot':
        return <span className="text-xs text-muted-foreground">captures the current accessibility tree</span>
      case 'extract':
        return (
          <Select value={action.format ?? 'markdown'} onValueChange={(v) => onChange({ ...action, format: v as never })}>
            <SelectTrigger className="w-40">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="markdown">markdown</SelectItem>
              <SelectItem value="text">text</SelectItem>
              <SelectItem value="html">html</SelectItem>
            </SelectContent>
          </Select>
        )
      case 'screenshot':
        return (
          <label className="flex items-center gap-2 text-xs">
            <input
              type="checkbox"
              checked={action.full_page ?? false}
              onChange={(e) => onChange({ ...action, full_page: e.target.checked })}
            />
            full page
          </label>
        )
      case 'wait':
        return (
          <Input
            type="number"
            placeholder="ms"
            value={action.ms ?? ''}
            onChange={(e) => onChange({ ...action, ms: e.target.value ? Number(e.target.value) : null })}
            className="w-28"
          />
        )
      case 'execute_js':
        return (
          <Input
            placeholder="document.title"
            value={action.script}
            onChange={(e) => onChange({ ...action, script: e.target.value })}
          />
        )
      case 'click':
        return (
          <label className="flex items-center gap-2 text-xs">
            <input
              type="checkbox"
              checked={action.all ?? false}
              onChange={(e) => onChange({ ...action, all: e.target.checked })}
            />
            all matches
          </label>
        )
      case 'fill':
        return (
          <Input
            placeholder="text to type"
            value={action.text}
            onChange={(e) => onChange({ ...action, text: e.target.value })}
          />
        )
      case 'select_option':
        return (
          <Input
            placeholder="comma-separated values"
            value={action.values.join(',')}
            onChange={(e) => onChange({ ...action, values: e.target.value.split(',').filter(Boolean) })}
          />
        )
      case 'hover':
        return <span className="text-xs text-muted-foreground">hovers the picked element</span>
      case 'press':
        return (
          <Input
            placeholder="Enter"
            value={action.key}
            onChange={(e) => onChange({ ...action, key: e.target.value })}
            className="w-32"
          />
        )
      case 'scroll':
        return (
          <Select
            value={action.direction}
            onValueChange={(v) => onChange({ ...action, direction: v } as ActionIn)}
          >
            <SelectTrigger className="w-32">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="up">up</SelectItem>
              <SelectItem value="down">down</SelectItem>
              <SelectItem value="left">left</SelectItem>
              <SelectItem value="right">right</SelectItem>
            </SelectContent>
          </Select>
        )
      case 'new_tab':
        return (
          <Input
            placeholder="https://example.com (optional)"
            value={action.url ?? ''}
            onChange={(e) => onChange({ ...action, url: e.target.value || null })}
          />
        )
      case 'close_tab':
      case 'switch_tab':
        return (
          <Input
            placeholder="page_id"
            value={action.page_id}
            onChange={(e) => onChange({ ...action, page_id: e.target.value })}
            className="w-40 font-mono"
          />
        )
      case 'list_tabs':
        return <span className="text-xs text-muted-foreground">lists this session's open tabs</span>
    }
  }

  return (
    <div className="flex items-start gap-2 rounded-md border border-border p-2">
      <div className="flex flex-col">
        <Button size="icon" variant="ghost" onClick={onMoveUp} className="h-5 w-5">
          <ArrowUp className="size-3" />
        </Button>
        <Button size="icon" variant="ghost" onClick={onMoveDown} className="h-5 w-5">
          <ArrowDown className="size-3" />
        </Button>
      </div>
      <div className="w-24 shrink-0 pt-1.5 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        {action.type}
      </div>
      <div className="flex flex-1 flex-wrap items-center gap-2">
        {needsRef && (
          <div className="flex items-center gap-1">
            <Input
              placeholder="ref (e.g. e12)"
              value={'ref' in action ? action.ref ?? '' : ''}
              onChange={(e) => onChange({ ...action, ref: e.target.value } as ActionIn)}
              className="w-28 font-mono"
            />
            <Button size="icon" variant="outline" onClick={onPickRef} title="Pick element from snapshot">
              <Crosshair className="size-3.5" />
            </Button>
          </div>
        )}
        {fields()}
      </div>
      <Button size="icon" variant="ghost" onClick={onRemove}>
        <Trash2 className="size-4" />
      </Button>
    </div>
  )
}
