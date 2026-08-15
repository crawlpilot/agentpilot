import {
  BookOpen,
  Bot,
  FileScan,
  Globe,
  KeyRound,
  Layers,
  LayoutDashboard,
  MousePointerClick,
  Route as RouteIcon,
  Server,
  type LucideIcon,
} from 'lucide-react'

// Single source of truth for the app's navigation surface. The sidebar
// (NavSidebar), the breadcrumb trail (TopBar), and the command palette
// (CommandPalette) all read from this list, so a route added here shows up
// in all three at once instead of being wired up three times and drifting.

export interface NavItem {
  to: string
  label: string
  icon: LucideIcon
  end?: boolean
  // `adminOnly` items need the platform admin token, not a tenant key --
  // they stay in the nav for admins and are filtered out for tenant-only
  // sessions (see NavSidebar's `isAdmin` gate).
  adminOnly?: boolean
  // Extra terms the command palette matches against beyond `label` -- e.g.
  // "extract" should find Scrape even though the label doesn't say it.
  keywords?: string[]
}

export interface NavGroup {
  label: string | null
  items: NavItem[]
}

export const NAV_GROUPS: NavGroup[] = [
  {
    label: null,
    items: [
      { to: '/', label: 'Dashboard', icon: LayoutDashboard, end: true, keywords: ['home', 'overview'] },
      { to: '/sessions', label: 'Sessions', icon: Globe, keywords: ['browser', 'context', 'pool'] },
      { to: '/recipes', label: 'Recipes', icon: BookOpen, keywords: ['extract', 'template', 'saved'] },
      { to: '/agent-runs', label: 'Agent runs', icon: Bot, keywords: ['ai', 'autonomous', 'task', 'history'] },
    ],
  },
  {
    label: 'Playground',
    items: [
      { to: '/playground/scrape', label: 'Scrape a web page', icon: FileScan, keywords: ['extract', 'markdown', 'html'] },
      { to: '/playground/map', label: 'Map a website', icon: RouteIcon, keywords: ['links', 'sitemap', 'urls'] },
      { to: '/playground/crawl', label: 'Crawl entire website', icon: Layers, keywords: ['spider', 'batch'] },
      { to: '/playground/interact', label: 'Interact with a page', icon: MousePointerClick, keywords: ['click', 'type', 'automate'] },
      { to: '/playground/agent', label: 'Run an agent', icon: Bot, keywords: ['ai', 'autonomous', 'task'] },
    ],
  },
  {
    label: null,
    items: [
      { to: '/nodes', label: 'Nodes', icon: Server, adminOnly: true, keywords: ['workers', 'capacity', 'fleet'] },
      { to: '/api-keys', label: 'API Keys', icon: KeyRound, adminOnly: true, keywords: ['tokens', 'credentials', 'auth'] },
    ],
  },
]

// Flat list, handy for the command palette and breadcrumb label lookups.
export const ALL_NAV_ITEMS: NavItem[] = NAV_GROUPS.flatMap((g) => g.items)

// Human labels for path segments the nav list doesn't name directly --
// dynamic detail routes and the Playground's own path segment. Used to build
// the breadcrumb trail from a raw pathname.
const SEGMENT_LABELS: Record<string, string> = {
  playground: 'Playground',
  sessions: 'Sessions',
  recipes: 'Recipes',
  'agent-runs': 'Agent runs',
  'api-keys': 'API Keys',
  nodes: 'Nodes',
  scrape: 'Scrape',
  map: 'Map',
  crawl: 'Crawl',
  interact: 'Interact',
  agent: 'Agent',
}

export interface Crumb {
  label: string
  to: string
}

// Turn `/sessions/abc123` into [{Sessions, /sessions}, {abc123, /sessions/abc123}].
// The last crumb is the current page (rendered non-interactive by the caller).
export function breadcrumbsFor(pathname: string): Crumb[] {
  const segments = pathname.split('/').filter(Boolean)
  const crumbs: Crumb[] = []
  let acc = ''
  for (const seg of segments) {
    acc += `/${seg}`
    crumbs.push({ label: SEGMENT_LABELS[seg] ?? seg, to: acc })
  }
  return crumbs
}
