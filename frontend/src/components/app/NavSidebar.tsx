import { NavLink } from 'react-router-dom'
import { NAV_GROUPS } from '@/lib/nav'
import { useAuth } from '@/lib/auth/AuthContext'
import { Logo } from '@/components/app/Logo'
import { AccountMenu } from '@/components/app/AccountMenu'
import { cn } from '@/lib/utils'

export function NavSidebar() {
  const { isAdmin } = useAuth()

  // Admin-only routes (Nodes, API Keys) need the platform admin token; hide
  // them entirely for a tenant-only session rather than showing links that
  // dead-end on a 401. A group that empties out this way is dropped too, so
  // no orphaned "PLATFORM" header hangs over nothing.
  const groups = NAV_GROUPS.map((g) => ({
    ...g,
    items: g.items.filter((i) => !i.adminOnly || isAdmin),
  })).filter((g) => g.items.length > 0)

  return (
    <aside className="hidden w-60 shrink-0 flex-col border-r border-border bg-card sm:flex">
      <div className="flex h-16 items-center border-b border-border/60 px-4">
        <Logo />
      </div>

      <nav className="flex flex-1 flex-col gap-5 overflow-y-auto px-3 py-2">
        {groups.map((group, i) => (
          <div key={group.label ?? i} className="flex flex-col gap-0.5">
            {group.label && (
              <p className="px-2.5 pb-1 text-xs font-medium uppercase tracking-wide text-muted-foreground">
                {group.label}
              </p>
            )}
            {group.items.map(({ to, label, icon: Icon, end }) => (
              <NavLink
                key={to}
                to={to}
                end={end}
                className={({ isActive }) =>
                  cn(
                    'group flex items-center gap-2.5 rounded-md px-2.5 py-2 text-sm font-medium transition-colors',
                    isActive
                      ? 'bg-accent-tint text-accent'
                      : 'text-muted-foreground hover:bg-muted hover:text-foreground',
                  )
                }
              >
                <Icon className="size-4 shrink-0" />
                <span className="truncate">{label}</span>
              </NavLink>
            ))}
          </div>
        ))}
      </nav>

      <div className="border-t border-border p-3">
        <AccountMenu />
      </div>
    </aside>
  )
}
