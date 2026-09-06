import { Plus, X } from 'lucide-react'
import { Input } from '@/components/ui/input'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { LOCATOR_KINDS, newLocator } from '@/lib/recipe/document'
import { STRUCTURED_KINDS, type Locator, type LocatorKind } from '@/lib/recipe/types'
import { cn } from '@/lib/utils'

interface Props {
  locator: Locator
  onChange: (next: Locator) => void
  /**
   * Action targets are a narrower set than read locators: the driver resolves
   * them through `querySelector`, so XPath, JSON paths and CSS-with-index are
   * all refused at run time. The editor says so up front instead of letting
   * the lint catch it later.
   */
  actionTarget?: boolean
  /** Nested `within` editors drop the scope control to stop infinite nesting. */
  depth?: number
}

export function LocatorEditor({ locator, onChange, actionTarget = false, depth = 0 }: Props) {
  const set = (patch: Partial<Locator>) => onChange({ ...locator, ...patch })
  const isStructured = STRUCTURED_KINDS.includes(locator.kind)
  const scopable = locator.kind === 'css' || locator.kind === 'xpath' || locator.kind === 'ax_role'

  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex flex-wrap items-center gap-1.5">
        <Select
          value={locator.kind}
          onValueChange={(kind) => onChange(newLocator(kind as LocatorKind))}
        >
          <SelectTrigger className="h-7 w-32 text-xs">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {LOCATOR_KINDS.map((kind) => (
              <SelectItem key={kind} value={kind}>
                {kind}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>

        {isStructured && (
          <>
            <Input
              className="h-7 min-w-56 flex-1 font-mono text-xs"
              placeholder={locator.kind === 'meta' ? 'og:title' : '[0].offers.price'}
              value={locator.path ?? ''}
              onChange={(e) => set({ path: e.target.value })}
            />
            <Select
              value={locator.path_lang ?? 'simple'}
              onValueChange={(v) => set({ path_lang: v as Locator['path_lang'] })}
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
        )}

        {(locator.kind === 'css' || locator.kind === 'xpath') && (
          <>
            <Input
              className="h-7 min-w-56 flex-1 font-mono text-xs"
              placeholder={locator.kind === 'css' ? '.product-price' : '//h1[@id="title"]'}
              value={locator.selector ?? ''}
              onChange={(e) => set({ selector: e.target.value })}
            />
            <Input
              className="h-7 w-28 font-mono text-xs"
              placeholder="attribute"
              title="text, visible_text, html, value, href, or any DOM attribute"
              value={locator.attribute ?? ''}
              onChange={(e) => set({ attribute: e.target.value || undefined })}
            />
          </>
        )}

        {locator.kind === 'ax_role' && (
          <>
            <Input
              className="h-7 w-28 font-mono text-xs"
              placeholder="role"
              value={locator.role ?? ''}
              onChange={(e) => set({ role: e.target.value })}
            />
            <Input
              className="h-7 min-w-40 flex-1 font-mono text-xs"
              placeholder="name contains"
              value={locator.name_contains ?? ''}
              onChange={(e) => set({ name_contains: e.target.value || null })}
            />
            <Input
              className="h-7 min-w-40 flex-1 font-mono text-xs"
              placeholder="name in (comma-separated)"
              value={locator.name_in?.join(', ') ?? ''}
              onChange={(e) => {
                const names = e.target.value.split(',').map((s) => s.trim()).filter(Boolean)
                set({ name_in: names.length ? names : null })
              }}
            />
          </>
        )}

        {locator.kind === 'text' && (
          <Input
            className="h-7 min-w-56 flex-1 text-xs"
            placeholder="visible text to match"
            value={locator.text ?? ''}
            onChange={(e) => set({ text: e.target.value })}
          />
        )}
      </div>

      {(locator.kind === 'css' || locator.kind === 'xpath') && !isStructured && (
        <div className="flex flex-wrap items-center gap-3 pl-1 text-xs text-muted-foreground">
          <label className="flex items-center gap-1.5">
            <input
              type="checkbox"
              checked={locator.all ?? false}
              onChange={(e) => set({ all: e.target.checked || undefined, index: undefined })}
            />
            all matches
          </label>
          <label className="flex items-center gap-1.5">
            index
            <Input
              type="number"
              className={cn('h-6 w-16 text-xs', actionTarget && locator.index != null && 'border-destructive')}
              value={locator.index ?? ''}
              onChange={(e) => set({ index: e.target.value === '' ? null : Number(e.target.value) })}
            />
          </label>
          {actionTarget && locator.index != null && locator.kind === 'css' && (
            <span className="text-destructive">
              a css action target cannot be indexed -- every iteration would hit the first match
            </span>
          )}
        </div>
      )}

      {scopable && depth === 0 && (
        <div className="pl-1">
          {locator.within ? (
            <div className="flex items-start gap-1.5 rounded-md border border-dashed border-border p-1.5">
              <Badge variant="outline" className="mt-0.5 shrink-0">
                within
              </Badge>
              <div className="min-w-0 flex-1">
                <LocatorEditor
                  locator={locator.within}
                  onChange={(within) => set({ within })}
                  depth={depth + 1}
                />
              </div>
              <Button size="icon" variant="ghost" className="size-6" onClick={() => set({ within: undefined })}>
                <X className="size-3" />
              </Button>
            </div>
          ) : (
            <Button
              size="sm"
              variant="ghost"
              className="h-6 px-1.5 text-xs text-muted-foreground"
              onClick={() => set({ within: newLocator(locator.kind === 'ax_role' ? 'css' : locator.kind) })}
              title="Resolve inside a container first, so a same-named element elsewhere on the page cannot match"
            >
              <Plus className="size-3" />
              scope to a container
            </Button>
          )}
        </div>
      )}
    </div>
  )
}
