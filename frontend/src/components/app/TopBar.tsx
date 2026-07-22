import { LogOut } from 'lucide-react'
import { useHealth } from '@/hooks/useHealth'
import { useAuth } from '@/lib/auth/AuthContext'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'

export function TopBar() {
  const { data, isError } = useHealth()
  const { tenant, logout } = useAuth()
  const healthy = !isError && data?.status === 'ok'

  return (
    <header className="flex h-14 shrink-0 items-center justify-between border-b border-border bg-card px-4">
      <div className="flex items-center gap-2 text-sm text-muted-foreground">
        <span
          className={cn('size-2 rounded-full', healthy ? 'bg-success' : 'bg-destructive')}
          aria-hidden
        />
        {healthy ? 'Gateway healthy' : 'Gateway unreachable'}
      </div>
      <div className="flex items-center gap-3">
        {tenant && (
          <span className="rounded-md bg-muted px-2 py-1 text-xs font-medium text-muted-foreground">
            tenant: {tenant}
          </span>
        )}
        <Button variant="ghost" size="sm" onClick={logout}>
          <LogOut className="size-4" />
          Sign out
        </Button>
      </div>
    </header>
  )
}
