import { Fragment } from 'react'
import { Link, useLocation } from 'react-router-dom'
import { ChevronRight, Search } from 'lucide-react'
import { useHealth } from '@/hooks/useHealth'
import { useCommandPalette } from '@/components/app/CommandPalette'
import { breadcrumbsFor } from '@/lib/nav'
import { cn } from '@/lib/utils'

// macOS shows ⌘, everything else Ctrl -- a tiny touch, but a Ctrl-K hint on a
// Mac (or vice-versa) reads as broken to the exact keyboard-first users who
// notice the hint at all.
const isMac = typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.platform)

export function TopBar() {
  const { data, isError } = useHealth()
  const { open } = useCommandPalette()
  const location = useLocation()
  const healthy = !isError && data?.status === 'ok'
  const crumbs = breadcrumbsFor(location.pathname)

  return (
    <header className="flex h-16 shrink-0 items-center justify-between gap-4 border-b border-border px-5">
      <nav aria-label="Breadcrumb" className="flex min-w-0 items-center gap-2 text-sm">
        <Link to="/" className="shrink-0 text-muted-foreground hover:text-foreground">
          Home
        </Link>
        {crumbs.map((crumb, i) => {
          const isLast = i === crumbs.length - 1
          return (
            <Fragment key={crumb.to}>
              <ChevronRight className="size-3.5 shrink-0 text-muted-foreground/60" aria-hidden />
              {isLast ? (
                <span className="truncate font-medium text-foreground">{crumb.label}</span>
              ) : (
                <Link to={crumb.to} className="truncate text-muted-foreground hover:text-foreground">
                  {crumb.label}
                </Link>
              )}
            </Fragment>
          )
        })}
      </nav>

      <div className="flex shrink-0 items-center gap-3">
        <button
          type="button"
          onClick={open}
          className="flex h-9 items-center gap-2 rounded-md border border-border bg-background px-3 text-sm text-muted-foreground transition-colors hover:bg-muted"
        >
          <Search className="size-4" />
          <span className="hidden sm:inline">Search...</span>
          <kbd className="hidden rounded border border-border bg-muted px-1.5 py-0.5 font-sans text-[11px] sm:inline">
            {isMac ? '⌘' : 'Ctrl'} K
          </kbd>
        </button>

        <div
          className="flex items-center gap-2 rounded-md border border-border bg-background px-2.5 py-1.5 text-xs font-medium text-muted-foreground"
          title={healthy ? 'Gateway healthy' : 'Gateway unreachable'}
        >
          <span
            className={cn('size-2 rounded-full', healthy ? 'bg-success' : 'bg-destructive')}
            aria-hidden
          />
          <span className="hidden md:inline">{healthy ? 'Gateway healthy' : 'Gateway unreachable'}</span>
        </div>
      </div>
    </header>
  )
}
