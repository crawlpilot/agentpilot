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
    <header className="flex h-12 shrink-0 items-center justify-between border-b border-border px-4">
      <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
        <span
          className={cn('size-1.5 rounded-full', healthy ? 'bg-success' : 'bg-destructive')}
          aria-hidden
        />
        {healthy ? 'Gateway healthy' : 'Gateway unreachable'}
      </div>
      <div className="flex items-center gap-2">
        {tenant && <span className="text-xs text-muted-foreground">{tenant}</span>}
        <Button variant="ghost" size="sm" onClick={logout}>
          <LogOut className="size-4" />
          Sign out
        </Button>
      </div>
    </header>
  )
}
