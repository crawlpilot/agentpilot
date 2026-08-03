import { useState } from 'react'
import { Check, Copy } from 'lucide-react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import type { DocumentOut } from '@/lib/api/types'

// Scoped rather than a Tailwind Typography plugin -- one component's worth
// of heading/link/code/table styling doesn't need a whole new dependency.
// `react-markdown` renders to real React elements (no `dangerouslySetInnerHTML`),
// which matters here specifically: this content comes from scraping
// arbitrary third-party pages, so raw-HTML injection is a real XSS surface,
// not just a hypothetical one.
const MARKDOWN_PREVIEW_CLASSES =
  'prose-preview max-h-96 overflow-auto p-3 text-sm leading-relaxed ' +
  '[&_h1]:mt-0 [&_h1]:mb-2 [&_h1]:text-lg [&_h1]:font-semibold ' +
  '[&_h2]:mt-4 [&_h2]:mb-2 [&_h2]:text-base [&_h2]:font-semibold ' +
  '[&_h3]:mt-3 [&_h3]:mb-1.5 [&_h3]:text-sm [&_h3]:font-semibold ' +
  '[&_p]:my-2 [&_ul]:my-2 [&_ul]:list-disc [&_ul]:pl-5 [&_ol]:my-2 [&_ol]:list-decimal [&_ol]:pl-5 ' +
  '[&_li]:my-0.5 [&_blockquote]:my-2 [&_blockquote]:border-l-2 [&_blockquote]:border-border ' +
  '[&_blockquote]:pl-3 [&_blockquote]:text-muted-foreground ' +
  '[&_a]:text-accent [&_a]:underline [&_a]:underline-offset-2 ' +
  '[&_img]:my-2 [&_img]:max-w-full [&_img]:rounded-md [&_img]:border [&_img]:border-border ' +
  '[&_code]:rounded [&_code]:bg-muted [&_code]:px-1 [&_code]:py-0.5 [&_code]:text-xs ' +
  '[&_pre]:my-2 [&_pre]:overflow-auto [&_pre]:rounded-md [&_pre]:bg-muted [&_pre]:p-2 [&_pre_code]:bg-transparent [&_pre_code]:p-0 ' +
  '[&_table]:my-2 [&_table]:border-collapse [&_table]:text-xs ' +
  '[&_th]:border [&_th]:border-border [&_th]:bg-muted [&_th]:px-2 [&_th]:py-1 [&_th]:text-left ' +
  '[&_td]:border [&_td]:border-border [&_td]:px-2 [&_td]:py-1 ' +
  '[&_hr]:my-3 [&_hr]:border-border'

function MarkdownPreview({ content }: { content: string }) {
  return (
    <div className={MARKDOWN_PREVIEW_CLASSES}>
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{content}</ReactMarkdown>
    </div>
  )
}

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

  // Markdown defaults to a rendered view -- raw markdown syntax (`#`, `[x](y)`,
  // `|---|`) is what prompted this change in the first place. HTML/text have
  // no analogous "rendered" mode here, so this only ever applies to markdown.
  const [markdownView, setMarkdownView] = useState<'rendered' | 'raw'>('rendered')

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
                  <div className="flex items-center justify-between border-b border-border px-2 py-1">
                    {f === 'markdown' ? (
                      <div className="flex gap-1">
                        <Button
                          size="sm"
                          variant={markdownView === 'rendered' ? 'secondary' : 'ghost'}
                          onClick={() => setMarkdownView('rendered')}
                        >
                          Rendered
                        </Button>
                        <Button
                          size="sm"
                          variant={markdownView === 'raw' ? 'secondary' : 'ghost'}
                          onClick={() => setMarkdownView('raw')}
                        >
                          Raw
                        </Button>
                      </div>
                    ) : (
                      <span />
                    )}
                    <CopyButton text={content ?? ''} />
                  </div>
                  {f === 'markdown' && markdownView === 'rendered' ? (
                    <MarkdownPreview content={content ?? ''} />
                  ) : (
                    <pre className="max-h-96 overflow-auto whitespace-pre-wrap p-3 text-xs">{content}</pre>
                  )}
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
