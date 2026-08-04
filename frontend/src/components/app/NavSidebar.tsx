import { NavLink } from 'react-router-dom'
import { BookOpen, FileScan, Globe, KeyRound, Layers, LayoutDashboard, MousePointerClick, Route as RouteIcon, Server } from 'lucide-react'
import { cn } from '@/lib/utils'

interface NavItem {
  to: string
  label: string
  icon: typeof LayoutDashboard
  end?: boolean
}

interface NavGroup {
  label: string | null
  items: NavItem[]
}

const NAV_GROUPS: NavGroup[] = [
  {
    label: null,
    items: [
      { to: '/', label: 'Dashboard', icon: LayoutDashboard, end: true },
      { to: '/sessions', label: 'Sessions', icon: Globe },
      { to: '/recipes', label: 'Recipes', icon: BookOpen },
    ],
  },
  {
    label: 'Playground',
    items: [
      { to: '/playground/scrape', label: 'Scrape a web page', icon: FileScan },
      { to: '/playground/map', label: 'Map a website', icon: RouteIcon },
      { to: '/playground/crawl', label: 'Crawl entire website', icon: Layers },
      { to: '/playground/interact', label: 'Interact with a page', icon: MousePointerClick },
    ],
  },
  {
    label: null,
    items: [
      { to: '/nodes', label: 'Nodes', icon: Server },
      { to: '/api-keys', label: 'API Keys', icon: KeyRound },
    ],
  },
]

export function NavSidebar() {
  return (
    <aside className="hidden w-56 shrink-0 border-r border-border bg-card p-4 sm:flex sm:flex-col">
      <div className="mb-8 flex items-center gap-2 px-2">
        <span className="size-2.5 rounded-full bg-accent" />
        <span className="text-sm font-semibold tracking-tight text-foreground">agentpilot</span>
      </div>
      <nav className="flex flex-col gap-4">
        {NAV_GROUPS.map((group, i) => (
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
                    'flex items-center gap-2.5 rounded-md px-2.5 py-2 text-sm font-medium transition-colors',
                    isActive
                      ? 'bg-accent-tint text-accent'
                      : 'text-muted-foreground hover:bg-muted hover:text-foreground',
                  )
                }
              >
                <Icon className="size-4" />
                {label}
              </NavLink>
            ))}
          </div>
        ))}
      </nav>
    </aside>
  )
}
