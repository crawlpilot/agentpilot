import { Link } from 'react-router-dom'
import { ArrowRight, type LucideIcon } from 'lucide-react'
import { Card } from '@/components/ui/card'

// A single "what do you want to do" tile on the dashboard -- the empty-state
// void the old dashboard left below its three stat cards was the biggest
// enterprise-polish gap, and a grid of these turns it into a launchpad
// (Firecrawl/browser-use both greet you with one).
export function QuickStartCard({
  to,
  title,
  description,
  icon: Icon,
}: {
  to: string
  title: string
  description: string
  icon: LucideIcon
}) {
  return (
    <Link to={to} className="group">
      <Card className="flex h-full items-start gap-3 p-4 transition-colors hover:border-accent/40 hover:bg-muted/40">
        <span className="grid size-9 shrink-0 place-items-center rounded-md bg-accent-tint text-accent">
          <Icon className="size-5" />
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1">
            <p className="text-sm font-semibold">{title}</p>
            <ArrowRight className="size-3.5 -translate-x-1 text-muted-foreground opacity-0 transition-all group-hover:translate-x-0 group-hover:opacity-100" />
          </div>
          <p className="mt-0.5 text-xs text-muted-foreground">{description}</p>
        </div>
      </Card>
    </Link>
  )
}
