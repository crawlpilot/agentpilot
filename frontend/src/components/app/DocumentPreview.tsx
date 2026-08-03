import { useState } from 'react'
import { Check, Copy } from 'lucide-react'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import type { DocumentOut } from '@/lib/api/types'

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false)
  return (
    <Button
      size="sm"
      variant="ghost"
      onClick={() => {
        navigator.clipboard.writeText(text)
        setCopied(true)
        setTimeout(() => setCopied(false), 1500)
      }}
    >
      {copied ? <Check className="size-3.5" /> : <Copy className="size-3.5" />}
      {copied ? 'Copied' : 'Copy'}
    </Button>
  )
}

// Shared by the Scrape tab (one document, top-level result) and the Crawl
// tab (one row's expanded preview) -- `DocumentOut` is the exact same shape
// in both `ScrapeResponse.data` and `CrawlStatusResponse.data[]`.
export function DocumentPreview({ document }: { document: DocumentOut }) {
  const formats = [
    document.markdown != null && ('markdown' as const),
    document.html != null && ('html' as const),
    document.text != null && ('text' as const),
  ].filter((f): f is 'markdown' | 'html' | 'text' => f !== false)

  return (
    <div className="flex flex-col gap-3">
      {document.error && (
        <div className="flex items-center gap-2 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm">
          <Badge variant="destructive">error</Badge>
          <span className="text-muted-foreground">{document.error}</span>
        </div>
      )}

      {document.metadata && (
        <div className="flex flex-wrap items-center gap-2">
          {document.metadata.status_code !== null && (
            <Badge variant="outline">{document.metadata.status_code}</Badge>
          )}
          <Badge variant="outline">tier: {document.metadata.tier_used}</Badge>
          <Badge variant="outline">{Math.round(document.metadata.duration_ms)}ms</Badge>
        </div>
      )}

      {document.screenshot && (
        <div className="overflow-hidden rounded-md border border-border">
          <img src={`data:image/png;base64,${document.screenshot}`} alt="screenshot" className="w-full" />
        </div>
      )}

      {formats.length > 0 && (
        <Tabs defaultValue={formats[0]}>
          <TabsList>
            {formats.map((f) => (
              <TabsTrigger key={f} value={f} className="capitalize">
                {f}
              </TabsTrigger>
            ))}
          </TabsList>
          {formats.map((f) => {
            const content = f === 'markdown' ? document.markdown : f === 'html' ? document.html : document.text
            return (
              <TabsContent key={f} value={f}>
                <div className="rounded-md border border-border">
                  <div className="flex items-center justify-end border-b border-border px-2 py-1">
                    <CopyButton text={content ?? ''} />
                  </div>
                  <pre className="max-h-96 overflow-auto whitespace-pre-wrap p-3 text-xs">{content}</pre>
                </div>
              </TabsContent>
            )
          })}
        </Tabs>
      )}

      {document.links.length > 0 && (
        <details className="text-sm text-muted-foreground">
          <summary className="cursor-pointer select-none">{document.links.length} links found</summary>
          <ul className="mt-2 flex flex-col gap-1">
            {document.links.map((link) => (
              <li key={link} className="truncate font-mono text-xs">
                {link}
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  )
}
