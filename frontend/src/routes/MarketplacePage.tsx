import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { PencilRuler, Search, Store } from 'lucide-react'
import { useTemplates } from '@/hooks/useMarketplace'
import { EmptyState } from '@/components/app/EmptyState'
import { Badge, type BadgeProps } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Skeleton } from '@/components/ui/skeleton'
import { useAuth } from '@/lib/auth/AuthContext'
import type { TemplateOut } from '@/lib/api/types'
import { cn } from '@/lib/utils'

/**
 * The scraper catalogue.
 *
 * This is a different list from `/recipes`, and the difference is the point.
 * `/recipes` is what *you* built, published or not; this is what is available
 * to run -- yours plus anything published to the tenant or publicly. The
 * server enforces that split in SQL (`list_templates`), so a recipe someone
 * kept private cannot appear here even by accident.
 *
 * A card answers the question somebody arriving here actually has: *what does
 * this give me?* That is the field list, so it is the largest thing on the
 * card -- not the recipe's name, which is whatever its author typed.
 */

function healthVariant(status: string): NonNullable<BadgeProps['variant']> {
  switch (status) {
    case 'healthy':
      return 'success'
    case 'degraded':
      return 'warning'
    case 'broken':
      return 'destructive'
    default:
      return 'outline'
  }
}

/** Fields worth showing on a card before it becomes a wall of chips. */
const FIELD_PREVIEW = 6

function TemplateCard({ template }: { template: TemplateOut }) {
  const extra = template.field_names.length - FIELD_PREVIEW
  return (
    <Link
      to={`/marketplace/${template.recipe_id}`}
      className={cn(
        'flex flex-col gap-3 rounded-lg border border-border bg-card p-4 transition-colors',
        'hover:border-accent/60 hover:bg-muted/40',
      )}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="truncate font-medium">{template.name}</p>
          <p className="truncate text-xs text-muted-foreground">
            {template.domain ?? 'any site'}
            {template.page_type ? ` · ${template.page_type}` : ''}
          </p>
        </div>
        <Badge variant={healthVariant(template.health_status)} className="shrink-0 capitalize">
          {template.health_status}
        </Badge>
      </div>

      <div className="flex flex-wrap gap-1">
        {template.field_names.slice(0, FIELD_PREVIEW).map((name) => (
          <Badge key={name} variant="outline" className="font-mono text-[10px]">
            {name}
          </Badge>
        ))}
        {extra > 0 && <Badge variant="outline" className="text-[10px]">+{extra} more</Badge>}
        {template.field_names.length === 0 && (
          <span className="text-xs text-muted-foreground">No fields declared</span>
        )}
      </div>

      <div className="mt-auto flex items-center justify-between text-[11px] text-muted-foreground">
        <span>v{template.version}</span>
        <span>Updated {new Date(template.updated_at).toLocaleDateString()}</span>
      </div>
    </Link>
  )
}

export function MarketplacePage() {
  const { isAuthed } = useAuth()
  const { data, isLoading, isError } = useTemplates()
  const [query, setQuery] = useState('')

  const templates = useMemo(() => data?.templates ?? [], [data])

  // Filtering client-side rather than round-tripping: the endpoint filters by
  // exact `domain`/`page_type`, which is the wrong shape for a search box
  // where somebody types half a word. The catalogue is capped at 200 rows, so
  // there is nothing to gain by asking the server.
  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return templates
    return templates.filter((t) =>
      [t.name, t.domain ?? '', t.page_type ?? '', ...t.field_names]
        .join(' ')
        .toLowerCase()
        .includes(q),
    )
  }, [templates, query])

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h1 className="text-xl font-semibold">Scraper marketplace</h1>
          <p className="text-sm text-muted-foreground">
            Prebuilt extraction recipes. Pick one, give it your URLs, get structured data back.
          </p>
        </div>
        <Button asChild variant="outline">
          <Link to="/recipes/new">
            <PencilRuler className="size-4" />
            Build your own
          </Link>
        </Button>
      </div>

      {!isAuthed ? (
        <EmptyState
          icon={<Store className="size-8" />}
          title="Tenant API key required"
          description="Sign in with a tenant API key to browse the catalogue -- an admin token alone doesn't carry tenant scope."
        />
      ) : isLoading ? (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {[0, 1, 2, 3, 4, 5].map((i) => (
            <Skeleton key={i} className="h-36" />
          ))}
        </div>
      ) : isError ? (
        <EmptyState
          title="Couldn't load the catalogue"
          description="Check that the gateway is reachable and your api key is valid."
        />
      ) : templates.length === 0 ? (
        <EmptyState
          icon={<Store className="size-8" />}
          title="Nothing published yet"
          description="A recipe appears here once its author sets its visibility to the tenant or to everyone. Yours are private until you choose otherwise."
          action={
            <Button asChild>
              <Link to="/recipes/new">
                <PencilRuler className="size-4" />
                Build the first one
              </Link>
            </Button>
          }
        />
      ) : (
        <>
          <div className="relative max-w-sm">
            <Search className="pointer-events-none absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
            <Input
              className="pl-8"
              placeholder="Search by site, page type or field"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
          </div>

          {filtered.length === 0 ? (
            <EmptyState
              title={`Nothing matches "${query}"`}
              description="Search covers the recipe name, the site, the page type and every field it returns."
            />
          ) : (
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
              {filtered.map((template) => (
                <TemplateCard key={template.recipe_id} template={template} />
              ))}
            </div>
          )}
        </>
      )}
    </div>
  )
}
