import { Outlet, useLocation, useNavigate } from 'react-router-dom'
import { Bot, FileScan, Layers, MousePointerClick, Route as RouteIcon } from 'lucide-react'
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs'

const TABS = [
  { value: 'scrape', label: 'Scrape', icon: FileScan },
  { value: 'map', label: 'Map', icon: RouteIcon },
  { value: 'crawl', label: 'Crawl', icon: Layers },
  { value: 'interact', label: 'Interact', icon: MousePointerClick },
  { value: 'agent', label: 'Agent', icon: Bot },
] as const

// The shell: header + a tab strip whose value is derived from the current
// route (not local state) + `<Outlet/>` for the active tool's own page --
// see router.tsx's nested `playground` children. A flat tab bar rather than
// the reference screenshot's DISCOVER/EXTRACT/CRAWL category groups: with
// these tools (Search/Parse/Monitor have no backend yet, scoped out of this
// pass), single-item category headers would add noise, not clarity.
export function PlaygroundPage() {
  const location = useLocation()
  const navigate = useNavigate()
  const activeTab = location.pathname.split('/')[2] ?? 'scrape'

  return (
    <div className="flex flex-col gap-4">
      <div>
        <h1 className="text-xl font-semibold">Playground</h1>
        <p className="text-sm text-muted-foreground">Try the API directly in your browser.</p>
      </div>

      <Tabs value={activeTab} onValueChange={(v) => navigate(`/playground/${v}`)}>
        <TabsList>
          {TABS.map(({ value, label, icon: Icon }) => (
            <TabsTrigger key={value} value={value} className="gap-1.5">
              <Icon className="size-3.5" />
              {label}
            </TabsTrigger>
          ))}
        </TabsList>
      </Tabs>

      <Outlet />
    </div>
  )
}
