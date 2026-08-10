import { useEffect, useState, type FormEvent } from 'react'
import { useSearchParams } from 'react-router-dom'
import { Layers } from 'lucide-react'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Button } from '@/components/ui/button'
import { Badge, type BadgeProps } from '@/components/ui/badge'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { EmptyState } from '@/components/app/EmptyState'
import { DocumentPreview } from '@/components/app/DocumentPreview'
import { ScrapeOptionsFields, DEFAULT_SCRAPE_OPTIONS, type ScrapeOptionsValue } from '@/components/app/ScrapeOptionsFields'
import { StringListField } from '@/components/app/StringListField'
import { JsonTextareaField } from '@/components/app/JsonTextareaField'
import { CopyButton } from '@/components/app/CopyButton'
import { RecentRunsList } from '@/components/app/RecentRunsList'
import { useCreateCrawl } from '@/hooks/useCreateCrawl'
import { useCrawlStatus } from '@/hooks/useCrawlStatus'
import { useCancelCrawl } from '@/hooks/useCancelCrawl'
import { useRecentRuns } from '@/hooks/useRecentRuns'
import { useAuth } from '@/lib/auth/AuthContext'
import { useToast } from '@/components/ui/toast'
import { getCrawlStatus } from '@/lib/api/crawl'
import type { CrawlJobStatus, DocumentOut, SitemapMode, WebhookEvent } from '@/lib/api/types'

const WEBHOOK_EVENTS: WebhookEvent[] = ['started', 'page', 'completed', 'failed']

function statusVariant(status: CrawlJobStatus): NonNullable<BadgeProps['variant']> {
  switch (status) {
    case 'completed':
      return 'success'
    case 'failed':
      return 'destructive'
    case 'scraping':
      return 'accent'
    case 'cancelled':
      return 'outline'
    default:
      return 'default' // 'queued'
  }
}

export function PlaygroundCrawlTab() {
  const [searchParams, setSearchParams] = useSearchParams()
  const [url, setUrl] = useState('')
  const [limit, setLimit] = useState(100)
  const [maxDiscoveryDepth, setMaxDiscoveryDepth] = useState<number | null>(null)
  const [allowExternalLinks, setAllowExternalLinks] = useState(false)
  const [allowSubdomains, setAllowSubdomains] = useState(false)
  const [allowBackwardCrawling, setAllowBackwardCrawling] = useState(false)
  const [ignoreRobotsTxt, setIgnoreRobotsTxt] = useState(false)
  const [sitemap, setSitemap] = useState<SitemapMode>('include')
  const [deduplicateSimilarUrls, setDeduplicateSimilarUrls] = useState(true)
  const [ignoreQueryParameters, setIgnoreQueryParameters] = useState(false)
  const [delayMs, setDelayMs] = useState<number | null>(null)
  const [maxConcurrency, setMaxConcurrency] = useState(10)
  const [includePaths, setIncludePaths] = useState<string[]>([])
  const [excludePaths, setExcludePaths] = useState<string[]>([])
  const [webhookUrl, setWebhookUrl] = useState('')
  const [webhookEvents, setWebhookEvents] = useState<WebhookEvent[]>([...WEBHOOK_EVENTS])
  const [webhookHeaders, setWebhookHeaders] = useState<Record<string, unknown> | null>(null)
  const [scrapeOptions, setScrapeOptions] = useState<ScrapeOptionsValue>(DEFAULT_SCRAPE_OPTIONS)

  const [jobId, setJobId] = useState<string | null>(searchParams.get('id'))
  const [webhookSecret, setWebhookSecret] = useState<string | null>(null)
  const [documents, setDocuments] = useState<Record<string, DocumentOut>>({})
  const [nextCursor, setNextCursor] = useState<string | null>(null)
  const [paginated, setPaginated] = useState(false)
  const [loadingMore, setLoadingMore] = useState(false)

  const { apiKey } = useAuth()
  const { toast } = useToast()
  const { runs, append, updateByJobId } = useRecentRuns('crawl')
  const createCrawl = useCreateCrawl()
  const cancelCrawl = useCancelCrawl()
  const { data: status } = useCrawlStatus(jobId)

  // Merge each poll's page-1 results into the accumulated document set by
  // id, rather than replacing it -- a poll only ever reflects "what's
  // completed so far, page 1," not an incremental delta, and once the user
  // has paginated further with "Load more" we stop letting a poll snap
  // `nextCursor` back to page 1's (see `paginated` below).
  useEffect(() => {
    if (!status) return
    setDocuments((prev) => {
      const next = { ...prev }
      for (const doc of status.data) next[doc.document_id] = doc
      return next
    })
    if (!paginated) setNextCursor(status.next)
    if (jobId) updateByJobId(jobId, { status: status.status })
  }, [status, paginated, jobId, updateByJobId])

  if (!apiKey) {
    return (
      <EmptyState
        title="Tenant API key required"
        description="Sign in with a tenant API key to crawl a site -- an admin token alone doesn't carry tenant scope."
      />
    )
  }

  function handleSubmit(e: FormEvent) {
    e.preventDefault()
    const trimmed = url.trim()
    if (!trimmed) return
    const trimmedWebhook = webhookUrl.trim()
    createCrawl.mutate(
      {
        url: trimmed,
        limit,
        max_discovery_depth: maxDiscoveryDepth,
        include_paths: includePaths,
        exclude_paths: excludePaths,
        allow_external_links: allowExternalLinks,
        allow_subdomains: allowSubdomains,
        allow_backward_crawling: allowBackwardCrawling,
        ignore_robots_txt: ignoreRobotsTxt,
        sitemap,
        deduplicate_similar_urls: deduplicateSimilarUrls,
        ignore_query_parameters: ignoreQueryParameters,
        delay_ms: delayMs,
        max_concurrency: maxConcurrency,
        scrape_options: scrapeOptions,
        webhook: trimmedWebhook
          ? {
              url: trimmedWebhook,
              events: webhookEvents,
              headers: (webhookHeaders as Record<string, string> | null) ?? {},
            }
          : null,
      },
      {
        onSuccess: (resp) => {
          setJobId(resp.id)
          setSearchParams({ id: resp.id })
          setDocuments({})
          setNextCursor(null)
          setPaginated(false)
          setWebhookSecret(resp.webhook_secret ?? null)
          append({
            endpoint: 'crawl',
            url: trimmed,
            formats: scrapeOptions.formats,
            status: 'queued',
            jobId: resp.id,
          })
        },
        onError: (err) => toast({ title: 'Crawl failed to start', description: err.message, variant: 'destructive' }),
      },
    )
  }

  function handleCancel() {
    if (!jobId) return
    cancelCrawl.mutate(jobId, {
      onSuccess: () => toast({ title: 'Crawl cancelled' }),
      onError: (err) => toast({ title: 'Cancel failed', description: err.message, variant: 'destructive' }),
    })
  }

  async function loadMore() {
    if (!jobId || !nextCursor || !apiKey) return
    setLoadingMore(true)
    try {
      const resp = await getCrawlStatus(apiKey, jobId, nextCursor)
      setDocuments((prev) => {
        const next = { ...prev }
        for (const doc of resp.data) next[doc.document_id] = doc
        return next
      })
      setPaginated(true)
      setNextCursor(resp.next)
    } catch (err) {
      toast({
        title: 'Failed to load more',
        description: err instanceof Error ? err.message : String(err),
        variant: 'destructive',
      })
    } finally {
      setLoadingMore(false)
    }
  }

  const documentList = Object.values(documents)
  const isRunning = status ? status.status === 'queued' || status.status === 'scraping' : false
  const pct = status && status.total > 0 ? Math.round(((status.completed + status.failed) / status.total) * 100) : 0

  return (
    <div className="flex flex-col gap-6">
      <form onSubmit={handleSubmit} className="flex flex-col gap-4">
        <div className="flex items-end gap-2">
          <div className="flex flex-1 flex-col gap-1.5">
            <Label htmlFor="crawl-url">URL</Label>
            <Input
              id="crawl-url"
              placeholder="https://example.com"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              autoFocus
            />
          </div>
          <Button type="submit" disabled={createCrawl.isPending || !url.trim()}>
            <Layers className="size-4" />
            {createCrawl.isPending ? 'Starting…' : 'Start crawling'}
          </Button>
        </div>

        <div className="flex flex-wrap items-end gap-4">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="crawl-limit">Limit</Label>
            <Input
              id="crawl-limit"
              type="number"
              min={1}
              className="w-28"
              value={limit}
              onChange={(e) => setLimit(Number(e.target.value) || 1)}
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="crawl-depth">Max discovery depth</Label>
            <Input
              id="crawl-depth"
              type="number"
              min={0}
              placeholder="unlimited"
              className="w-36"
              value={maxDiscoveryDepth ?? ''}
              onChange={(e) => setMaxDiscoveryDepth(e.target.value ? Number(e.target.value) : null)}
            />
          </div>
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={allowExternalLinks}
              onChange={(e) => setAllowExternalLinks(e.target.checked)}
            />
            Allow external links
          </label>
        </div>

        <details className="text-sm text-muted-foreground">
          <summary className="cursor-pointer select-none">Advanced options</summary>
          <div className="mt-3 flex flex-col gap-3 text-left">
            <div className="flex flex-wrap items-end gap-4">
              <label className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={allowSubdomains}
                  onChange={(e) => setAllowSubdomains(e.target.checked)}
                />
                Allow subdomains
              </label>
              <label className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={allowBackwardCrawling}
                  onChange={(e) => setAllowBackwardCrawling(e.target.checked)}
                />
                Allow backward crawling
              </label>
              <label className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={ignoreRobotsTxt}
                  onChange={(e) => setIgnoreRobotsTxt(e.target.checked)}
                />
                Ignore robots.txt
              </label>
              <label className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={deduplicateSimilarUrls}
                  onChange={(e) => setDeduplicateSimilarUrls(e.target.checked)}
                />
                Deduplicate similar URLs
              </label>
              <label className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={ignoreQueryParameters}
                  onChange={(e) => setIgnoreQueryParameters(e.target.checked)}
                />
                Ignore query parameters
              </label>
            </div>
            <div className="flex flex-wrap items-end gap-4">
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
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="crawl-delay">Delay between requests (ms)</Label>
                <Input
                  id="crawl-delay"
                  type="number"
                  min={0}
                  placeholder="none"
                  className="w-36"
                  value={delayMs ?? ''}
                  onChange={(e) => setDelayMs(e.target.value ? Number(e.target.value) : null)}
                />
              </div>
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="crawl-concurrency">Max concurrency</Label>
                <Input
                  id="crawl-concurrency"
                  type="number"
                  min={1}
                  className="w-28"
                  value={maxConcurrency}
                  onChange={(e) => setMaxConcurrency(Number(e.target.value) || 1)}
                />
              </div>
            </div>
            <div className="flex flex-col gap-3">
              <StringListField
                label="Include paths (regex, comma-separated)"
                value={includePaths}
                onChange={setIncludePaths}
                placeholder="/blog/.*, /docs/.*"
              />
              <StringListField
                label="Exclude paths (regex, comma-separated)"
                value={excludePaths}
                onChange={setExcludePaths}
                placeholder="/admin/.*, .*\\.pdf"
              />
            </div>

            <div className="flex flex-col gap-3 rounded-md border border-border p-3">
              <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                Webhook (optional)
              </p>
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="crawl-webhook-url">Callback URL</Label>
                <Input
                  id="crawl-webhook-url"
                  placeholder="https://your-server.example/hooks/crawl"
                  value={webhookUrl}
                  onChange={(e) => setWebhookUrl(e.target.value)}
                />
              </div>
              {webhookUrl.trim() && (
                <>
                  <div className="flex flex-col gap-1.5">
                    <Label>Events</Label>
                    <div className="flex flex-wrap gap-3">
                      {WEBHOOK_EVENTS.map((event) => (
                        <label key={event} className="flex items-center gap-1.5 text-sm">
                          <input
                            type="checkbox"
                            checked={webhookEvents.includes(event)}
                            onChange={(e) =>
                              setWebhookEvents((prev) =>
                                e.target.checked ? [...prev, event] : prev.filter((x) => x !== event),
                              )
                            }
                          />
                          {event}
                        </label>
                      ))}
                    </div>
                  </div>
                  <JsonTextareaField
                    label="Custom headers (optional)"
                    value={webhookHeaders}
                    onChange={setWebhookHeaders}
                    rows={3}
                    placeholder={'{\n  "Authorization": "Bearer …"\n}'}
                  />
                </>
              )}
            </div>

            <div className="rounded-md border border-border p-3">
              <p className="mb-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">
                Scrape options (applied per page)
              </p>
              <ScrapeOptionsFields value={scrapeOptions} onChange={setScrapeOptions} />
            </div>
          </div>
        </details>
      </form>

      {webhookSecret && (
        <div className="flex flex-col gap-2 rounded-md border border-border bg-muted p-3">
          <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Webhook signing secret</p>
          <p className="text-xs text-muted-foreground">
            Copy this now — it's shown once. Use it to verify the HMAC signature on incoming webhook deliveries.
          </p>
          <div className="flex items-center gap-2">
            <code className="flex-1 select-all break-all rounded bg-background px-2 py-1 font-mono text-xs">
              {webhookSecret}
            </code>
            <CopyButton text={webhookSecret} />
            <Button size="sm" variant="ghost" onClick={() => setWebhookSecret(null)}>
              Dismiss
            </Button>
          </div>
        </div>
      )}

      {status && (
        <div className="flex flex-col gap-2 rounded-md border border-border p-3">
          <div className="flex items-center gap-2">
            <Badge variant={statusVariant(status.status)} className="capitalize">
              {status.status}
            </Badge>
            <span className="text-sm text-muted-foreground">
              {status.completed} / {status.total} pages{status.failed > 0 ? `, ${status.failed} failed` : ''}
            </span>
            <div className="flex-1" />
            <Button size="sm" variant="outline" disabled={!isRunning || cancelCrawl.isPending} onClick={handleCancel}>
              {cancelCrawl.isPending ? 'Cancelling…' : 'Cancel'}
            </Button>
          </div>
          <div className="h-2 w-full overflow-hidden rounded-full bg-muted">
            <div className="h-full rounded-full bg-accent transition-all" style={{ width: `${pct}%` }} />
          </div>
        </div>
      )}

      <div className="flex flex-col gap-2">
        <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
          {documentList.length > 0 ? `${documentList.length} pages` : 'Result'}
        </p>
        {documentList.length === 0 ? (
          <EmptyState title="No result yet" description="Crawl a site to see completed pages here." />
        ) : (
          <div className="flex flex-col gap-2">
            {documentList.map((doc) => (
              <details key={doc.document_id} className="rounded-md border border-border p-3">
                <summary className="flex cursor-pointer select-none items-center gap-2 text-sm">
                  {doc.error ? <Badge variant="destructive">error</Badge> : <Badge variant="success">ok</Badge>}
                  <span className="flex-1 truncate font-mono text-xs">{doc.url}</span>
                </summary>
                <div className="mt-3">
                  <DocumentPreview document={doc} />
                </div>
              </details>
            ))}
            {nextCursor && (
              <Button variant="outline" size="sm" onClick={loadMore} disabled={loadingMore} className="w-fit">
                {loadingMore ? 'Loading…' : 'Load more'}
              </Button>
            )}
          </div>
        )}
      </div>

      <RecentRunsList runs={runs} />
    </div>
  )
}
