import { AlertCircle, AlertTriangle, CheckCircle2, Info } from 'lucide-react'
import type { LintIssue, LintSeverity } from '@/lib/recipe/lint'
import { cn } from '@/lib/utils'

const ICONS: Record<LintSeverity, typeof Info> = {
  error: AlertCircle,
  warning: AlertTriangle,
  info: Info,
}

const COLORS: Record<LintSeverity, string> = {
  error: 'text-destructive',
  warning: 'text-warning',
  info: 'text-muted-foreground',
}

const ORDER: LintSeverity[] = ['error', 'warning', 'info']

export function LintPanel({ issues, onJump }: { issues: LintIssue[]; onJump: (tab: string) => void }) {
  if (issues.length === 0) {
    return (
      <div className="flex items-center gap-2 p-3 text-xs text-muted-foreground">
        <CheckCircle2 className="size-3.5 text-success" />
        Nothing to flag. That is not the same as verified -- run it against a page.
      </div>
    )
  }

  const sorted = [...issues].sort((a, b) => ORDER.indexOf(a.severity) - ORDER.indexOf(b.severity))

  return (
    <div className="flex flex-col divide-y divide-border/60 overflow-y-auto">
      {sorted.map((issue, i) => {
        const Icon = ICONS[issue.severity]
        return (
          <button
            key={i}
            type="button"
            onClick={() => issue.tab && onJump(issue.tab)}
            className={cn(
              'flex items-start gap-2 px-3 py-2 text-left text-xs',
              issue.tab && 'hover:bg-muted',
            )}
          >
            <Icon className={cn('mt-0.5 size-3.5 shrink-0', COLORS[issue.severity])} />
            <div className="min-w-0">
              <div className="font-mono text-[11px] text-muted-foreground">{issue.path}</div>
              <div>{issue.message}</div>
            </div>
          </button>
        )
      })}
    </div>
  )
}
