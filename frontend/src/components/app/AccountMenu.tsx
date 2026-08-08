import { ChevronsUpDown, LifeBuoy, LogOut, ShieldCheck } from 'lucide-react'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'
import { useAuth } from '@/lib/auth/AuthContext'

// The signed-in identity block, docked at the foot of the sidebar the way
// Linear/Vercel/Firecrawl do it -- rather than a lone "Sign out" button
// floating in the top bar. Shows which tenant (and/or admin scope) the
// current credentials resolve to, and hangs sign-out + a docs link off a
// dropdown so the footer itself stays a single quiet row.
export function AccountMenu() {
  const { tenant, isAdmin, logout } = useAuth()

  // Prefer the tenant name as the primary identity; fall back to an
  // admin-only label when signed in with just the platform admin token.
  const primary = tenant ?? 'Platform admin'
  const secondary = tenant ? (isAdmin ? 'Tenant + admin' : 'Tenant') : 'Admin token'
  const initial = primary.charAt(0).toUpperCase()

  return (
    <DropdownMenu>
      <DropdownMenuTrigger className="flex w-full items-center gap-3 rounded-md px-2 py-2 text-left transition-colors hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
        <span className="grid size-9 shrink-0 place-items-center rounded-md bg-accent-tint text-sm font-semibold text-accent">
          {initial}
        </span>
        <span className="min-w-0 flex-1">
          <span className="block truncate text-sm font-semibold text-foreground">{primary}</span>
          <span className="block truncate text-xs text-muted-foreground">{secondary}</span>
        </span>
        <ChevronsUpDown className="size-4 shrink-0 text-muted-foreground" />
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" side="top" className="w-56">
        {isAdmin && (
          <DropdownMenuItem className="text-muted-foreground" disabled>
            <ShieldCheck className="size-4" />
            Admin access
          </DropdownMenuItem>
        )}
        <DropdownMenuItem asChild>
          <a href="https://github.com/rahulbisht6365/baas-crawlpilot#readme" target="_blank" rel="noreferrer">
            <LifeBuoy className="size-4" />
            Documentation
          </a>
        </DropdownMenuItem>
        <DropdownMenuItem variant="destructive" onSelect={logout}>
          <LogOut className="size-4" />
          Sign out
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
