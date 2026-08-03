import { useState, type FormEvent } from 'react'
import { ExternalLink, Route as RouteIcon } from 'lucide-react'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Button } from '@/components/ui/button'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { EmptyState } from '@/components/app/EmptyState'
import { RecentRunsList } from '@/components/app/RecentRunsList'
import { useMap } from '@/hooks/useMap'
import { useRecentRuns } from '@/hooks/useRecentRuns'
import { useToast } from '@/components/ui/toast'
import { useAuth } from '@/lib/auth/AuthContext'
import type { SitemapMode } from '@/lib/api/types'

function parseCommaList(value: string): string[] {
  return value
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean)
}

export function PlaygroundMapTab() {
  const { apiKey } = useAuth()
  const [url, setUrl] = useState('')
  const [limit, setLimit] = useState(100)
  const [sitemap, setSitemap] = useState<SitemapMode>('include')
  const [includeSubdomains, setIncludeSubdomains] = useState(false)
  const [ignoreQueryParameters, setIgnoreQueryParameters] = useState(false)
  const [includePaths, setIncludePaths] = useState('')
  const [excludePaths, setExcludePaths] = useState('')
  const map = useMap()
  const { toast } = useToast()
  const { runs, append } = useRecentRuns('map')

  if (!apiKey) {
    return (
      <EmptyState
        title="Tenant API key required"
        description="Sign in with a tenant API key to map a site -- an admin token alone doesn't carry tenant scope."
      />
    )
  }

  function handleSubmit(e: FormEvent) {
    e.preventDefault()
    const trimmed = url.trim()
    if (!trimmed) return
    map.mutate(
      {
        url: trimmed,
        limit,
        sitemap,
        include_subdomains: includeSubdomains,
        ignore_query_parameters: ignoreQueryParameters,
        include_paths: parseCommaList(includePaths),
        exclude_paths: parseCommaList(excludePaths),
      },
      {
        onSuccess: () => append({ endpoint: 'map', url: trimmed, status: 'success' }),
        onError: (err) => {
          toast({ title: 'Map failed', description: err.message, variant: 'destructive' })
          append({ endpoint: 'map', url: trimmed, status: 'error' })
        },
      },
    )
  }

  return (
    <div className="flex flex-col gap-6">
      <form onSubmit={handleSubmit} className="flex flex-col gap-4">
        <div className="flex items-end gap-2">
          <div className="flex flex-1 flex-col gap-1.5">
            <Label htmlFor="map-url">URL</Label>
            <Input
              id="map-url"
              placeholder="https://example.com"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              autoFocus
            />
          </div>
          <Button type="submit" disabled={map.isPending || !url.trim()}>
            <RouteIcon className="size-4" />
            {map.isPending ? 'Mapping…' : 'Start mapping'}
          </Button>
        </div>

        <div className="flex flex-wrap items-end gap-4">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="map-limit">Limit</Label>
            <Input
              id="map-limit"
              type="number"
              min={1}
              className="w-32"
              value={limit}
              onChange={(e) => setLimit(Number(e.target.value) || 1)}
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label>Sitemap</Label>
            <Select value={sitemap} onValueChange={(v) => setSitemap(v as SitemapMode)}>
              <SelectTrigger className="w-36">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="include">include</SelectItem>
                <SelectItem value="only">only</SelectItem>
                <SelectItem value="skip">skip</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={includeSubdomains}
              onChange={(e) => setIncludeSubdomains(e.target.checked)}
            />
            Include subdomains
          </label>
        </div>

        <details className="text-sm text-muted-foreground">
          <summary className="cursor-pointer select-none">Advanced options</summary>
          <div className="mt-2 flex flex-col gap-3 text-left">
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="map-include-paths">Include paths (comma-separated regex)</Label>
              <Input
                id="map-include-paths"
                placeholder="^/blog/"
                value={includePaths}
                onChange={(e) => setIncludePaths(e.target.value)}
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="map-exclude-paths">Exclude paths (comma-separated regex)</Label>
              <Input
                id="map-exclude-paths"
                placeholder="/drafts/"
                value={excludePaths}
                onChange={(e) => setExcludePaths(e.target.value)}
              />
            </div>
            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={ignoreQueryParameters}
                onChange={(e) => setIgnoreQueryParameters(e.target.checked)}
              />
              Ignore query parameters
            </label>
          </div>
        </details>
      </form>

      <div className="flex flex-col gap-2">
        <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
          {map.data ? `${map.data.links.length} links found` : 'Result'}
        </p>
        {map.data ? (
          map.data.links.length === 0 ? (
            <EmptyState title="No links found" description="Try a broader sitemap mode or a different URL." />
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>URL</TableHead>
                  <TableHead>Title</TableHead>
                  <TableHead>Description</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {map.data.links.map((link) => (
                  <TableRow key={link.url}>
                    <TableCell className="font-mono text-xs">
                      <a
                        href={link.url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="inline-flex items-center gap-1 text-accent hover:underline"
                      >
                        {link.url}
                        <ExternalLink className="size-3 shrink-0" />
                      </a>
                    </TableCell>
                    <TableCell className="text-muted-foreground">{link.title ?? '--'}</TableCell>
                    <TableCell className="max-w-xs truncate text-muted-foreground">
                      {link.description ?? '--'}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )
        ) : (
          <EmptyState title="No result yet" description="Map a site to see discovered links here." />
        )}
      </div>

      <RecentRunsList runs={runs} />
    </div>
  )
}
