import { NavLink } from 'react-router-dom'
import { Globe, KeyRound, LayoutDashboard, PlaySquare } from 'lucide-react'
import { cn } from '@/lib/utils'

const NAV_ITEMS = [
  { to: '/', label: 'Dashboard', icon: LayoutDashboard, end: true },
  { to: '/sessions', label: 'Sessions', icon: Globe },
  { to: '/playground', label: 'Playground', icon: PlaySquare },
  { to: '/api-keys', label: 'API Keys', icon: KeyRound },
]

export function NavSidebar() {
  return (
    <aside className="hidden w-56 shrink-0 border-r border-border bg-card p-4 sm:flex sm:flex-col">
      <div className="mb-8 flex items-center gap-2 px-2">
        <span className="size-2.5 rounded-full bg-accent" />
        <span className="text-sm font-semibold tracking-tight text-foreground">baas-crawlpilot</span>
      </div>
      <nav className="flex flex-col gap-0.5">
        {NAV_ITEMS.map(({ to, label, icon: Icon, end }) => (
          <NavLink
            key={to}
            to={to}
            end={end}
            className={({ isActive }) =>
              cn(
                'flex items-center gap-2.5 rounded-md px-2.5 py-2 text-sm font-medium transition-colors',
                isActive ? 'bg-accent-tint text-accent' : 'text-muted-foreground hover:bg-muted hover:text-foreground',
              )
            }
          >
            <Icon className="size-4" />
            {label}
          </NavLink>
        ))}
      </nav>
    </aside>
  )
}
