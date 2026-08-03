import { useState, type FormEvent } from 'react'
import { Play } from 'lucide-react'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Button } from '@/components/ui/button'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { EmptyState } from '@/components/app/EmptyState'
import { DocumentPreview } from '@/components/app/DocumentPreview'
import {
  DEFAULT_SCRAPE_OPTIONS,
  ScrapeOptionsFields,
  type ScrapeOptionsValue,
} from '@/components/app/ScrapeOptionsFields'
import { RecentRunsList } from '@/components/app/RecentRunsList'
import { useScrape } from '@/hooks/useScrape'
import { useRecentRuns } from '@/hooks/useRecentRuns'
import { useToast } from '@/components/ui/toast'
import { useAuth } from '@/lib/auth/AuthContext'
import type { Tier } from '@/lib/api/types'

export function PlaygroundScrapeTab() {
  const { apiKey } = useAuth()
  const [url, setUrl] = useState('')
  const [tier, setTier] = useState<Tier>('auto')
  const [options, setOptions] = useState<ScrapeOptionsValue>(DEFAULT_SCRAPE_OPTIONS)
  const scrape = useScrape()
  const { toast } = useToast()
  const { runs, append } = useRecentRuns('scrape')

  if (!apiKey) {
    return (
      <EmptyState
        title="Tenant API key required"
        description="Sign in with a tenant API key to scrape a page -- an admin token alone doesn't carry tenant scope."
      />
    )
  }

  function handleSubmit(e: FormEvent) {
    e.preventDefault()
    const trimmed = url.trim()
    if (!trimmed) return
    scrape.mutate(
      { url: trimmed, tier, ...options },
      {
        onSuccess: (resp) =>
          append({
            endpoint: 'scrape',
            url: trimmed,
            formats: options.formats,
            status: resp.data.error ? 'error' : 'success',
          }),
        onError: (err) => {
          toast({ title: 'Scrape failed', description: err.message, variant: 'destructive' })
          append({ endpoint: 'scrape', url: trimmed, formats: options.formats, status: 'error' })
        },
      },
    )
  }

  return (
    <div className="flex flex-col gap-6">
      <form onSubmit={handleSubmit} className="flex flex-col gap-4">
        <div className="flex items-end gap-2">
          <div className="flex flex-1 flex-col gap-1.5">
            <Label htmlFor="scrape-url">URL</Label>
            <Input
              id="scrape-url"
              placeholder="https://example.com"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              autoFocus
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label>Tier</Label>
            <Select value={tier} onValueChange={(v) => setTier(v as Tier)}>
              <SelectTrigger className="w-32">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="auto">auto</SelectItem>
                <SelectItem value="basic">basic</SelectItem>
                <SelectItem value="stealth">stealth</SelectItem>
                <SelectItem value="enhanced">enhanced</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <Button type="submit" disabled={scrape.isPending || !url.trim()}>
            <Play className="size-4" />
            {scrape.isPending ? 'Scraping…' : 'Start scraping'}
          </Button>
        </div>
        <ScrapeOptionsFields value={options} onChange={setOptions} />
      </form>

      <div className="flex flex-col gap-2">
        <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Result</p>
        {scrape.data ? (
          <DocumentPreview document={scrape.data.data} />
        ) : (
          <EmptyState title="No result yet" description="Scrape a URL to see the output here." />
        )}
      </div>

      <RecentRunsList runs={runs} />
    </div>
  )
}
