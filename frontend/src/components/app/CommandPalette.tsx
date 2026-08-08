import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { useNavigate } from 'react-router-dom'
import { CornerDownLeft, LogOut, Search } from 'lucide-react'
import { Dialog, DialogContent } from '@/components/ui/dialog'
import { ALL_NAV_ITEMS } from '@/lib/nav'
import { useAuth } from '@/lib/auth/AuthContext'
import { cn } from '@/lib/utils'

// A ⌘K/Ctrl-K command palette for keyboard-first navigation -- the pattern
// every modern operator console (Linear, Vercel, Firecrawl) leans on. Built
// on the existing Radix Dialog rather than pulling in `cmdk`: the surface
// here is a filtered nav list + a couple of actions, not enough to justify a
// dependency. Exposed app-wide via context so the TopBar search affordance
// and the global hotkey open the same instance.

interface CommandItem {
  id: string
  label: string
  group: string
  icon: typeof Search
  keywords?: string[]
  run: () => void
}

interface CommandPaletteContextValue {
  open: () => void
}

const CommandPaletteContext = createContext<CommandPaletteContextValue | null>(null)

export function useCommandPalette(): CommandPaletteContextValue {
  const ctx = useContext(CommandPaletteContext)
  if (!ctx) throw new Error('useCommandPalette must be used within a CommandPaletteProvider')
  return ctx
}

export function CommandPaletteProvider({ children }: { children: ReactNode }) {
  const [isOpen, setIsOpen] = useState(false)
  const [query, setQuery] = useState('')
  const [activeIndex, setActiveIndex] = useState(0)
  const navigate = useNavigate()
  const { isAdmin, logout } = useAuth()
  const listRef = useRef<HTMLDivElement>(null)

  const open = useCallback(() => setIsOpen(true), [])

  // Global hotkey. Ignored while typing in a field *other* than our own
  // input, so ⌘K inside the palette's search box (or a form) still means
  // "select all" / whatever the field expects -- except we own ⌘K, so we
  // always intercept it; the guard is really about not stealing focus mid-edit.
  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault()
        setIsOpen((v) => !v)
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [])

  const items = useMemo<CommandItem[]>(() => {
    const navItems: CommandItem[] = ALL_NAV_ITEMS.filter((i) => !i.adminOnly || isAdmin).map((i) => ({
      id: `nav:${i.to}`,
      label: i.label,
      group: 'Navigate',
      icon: i.icon,
      keywords: i.keywords,
      run: () => navigate(i.to),
    }))
    const actions: CommandItem[] = [
      { id: 'action:signout', label: 'Sign out', group: 'Actions', icon: LogOut, keywords: ['logout', 'exit'], run: logout },
    ]
    return [...navItems, ...actions]
  }, [isAdmin, navigate, logout])

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return items
    return items.filter((i) => {
      const haystack = [i.label, i.group, ...(i.keywords ?? [])].join(' ').toLowerCase()
      return haystack.includes(q)
    })
  }, [items, query])

  // Reset transient state each time the palette opens, and keep the active
  // row in range as the filter narrows the list.
  useEffect(() => {
    if (isOpen) {
      setQuery('')
      setActiveIndex(0)
    }
  }, [isOpen])

  useEffect(() => {
    setActiveIndex((i) => Math.min(i, Math.max(0, filtered.length - 1)))
  }, [filtered.length])

  const runItem = useCallback(
    (item: CommandItem | undefined) => {
      if (!item) return
      setIsOpen(false)
      item.run()
    },
    [],
  )

  function onInputKeyDown(e: React.KeyboardEvent) {
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      setActiveIndex((i) => Math.min(i + 1, filtered.length - 1))
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      setActiveIndex((i) => Math.max(i - 1, 0))
    } else if (e.key === 'Enter') {
      e.preventDefault()
      runItem(filtered[activeIndex])
    }
  }

  // Keep the highlighted row scrolled into view during keyboard traversal.
  useEffect(() => {
    const el = listRef.current?.querySelector<HTMLElement>(`[data-index="${activeIndex}"]`)
    el?.scrollIntoView({ block: 'nearest' })
  }, [activeIndex])

  const value = useMemo(() => ({ open }), [open])

  let lastGroup: string | null = null

  return (
    <CommandPaletteContext.Provider value={value}>
      {children}
      <Dialog open={isOpen} onOpenChange={setIsOpen}>
        <DialogContent
          showClose={false}
          className="top-24 max-w-xl translate-y-0 gap-0 overflow-hidden p-0"
          aria-describedby={undefined}
        >
          <div className="flex items-center gap-2.5 border-b border-border px-4">
            <Search className="size-4 shrink-0 text-muted-foreground" aria-hidden />
            <input
              autoFocus
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={onInputKeyDown}
              placeholder="Search pages and actions..."
              className="h-12 flex-1 bg-transparent text-sm outline-none placeholder:text-muted-foreground"
              aria-label="Command palette search"
            />
            <kbd className="rounded border border-border bg-muted px-1.5 py-0.5 text-[10px] font-medium text-muted-foreground">
              ESC
            </kbd>
          </div>
          <div ref={listRef} className="max-h-80 overflow-y-auto p-1.5">
            {filtered.length === 0 ? (
              <p className="px-3 py-6 text-center text-sm text-muted-foreground">No matches for "{query}"</p>
            ) : (
              filtered.map((item, index) => {
                const showGroup = item.group !== lastGroup
                lastGroup = item.group
                const Icon = item.icon
                return (
                  <div key={item.id}>
                    {showGroup && (
                      <p className="px-2.5 pb-1 pt-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">
                        {item.group}
                      </p>
                    )}
                    <button
                      type="button"
                      data-index={index}
                      onMouseMove={() => setActiveIndex(index)}
                      onClick={() => runItem(item)}
                      className={cn(
                        'flex w-full items-center gap-2.5 rounded-md px-2.5 py-2 text-left text-sm',
                        index === activeIndex ? 'bg-accent-tint text-accent' : 'text-foreground',
                      )}
                    >
                      <Icon className="size-4 shrink-0" />
                      <span className="flex-1 truncate">{item.label}</span>
                      {index === activeIndex && <CornerDownLeft className="size-3.5 text-muted-foreground" />}
                    </button>
                  </div>
                )
              })
            )}
          </div>
        </DialogContent>
      </Dialog>
    </CommandPaletteContext.Provider>
  )
}
