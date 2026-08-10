import { Label } from '@/components/ui/label'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { JsonTextareaField } from '@/components/app/JsonTextareaField'
import { StringListField } from '@/components/app/StringListField'
import type { ExtractConfigIn, ScrapeFormat } from '@/lib/api/types'

export interface ScrapeOptionsValue {
  formats: ScrapeFormat[]
  only_main_content: boolean
  include_tags: string[]
  exclude_tags: string[]
  screenshot: boolean
  full_page_screenshot: boolean
  timeout_ms: number
  wait_for_ms: number | null
  extract: ExtractConfigIn | null
}

export const DEFAULT_SCRAPE_OPTIONS: ScrapeOptionsValue = {
  formats: ['markdown'],
  only_main_content: true,
  include_tags: [],
  exclude_tags: [],
  screenshot: false,
  full_page_screenshot: false,
  timeout_ms: 30_000,
  wait_for_ms: null,
  extract: null,
}

const FORMAT_OPTIONS: { value: ScrapeFormat; label: string }[] = [
  { value: 'markdown', label: 'Markdown' },
  { value: 'html', label: 'HTML' },
  { value: 'text', label: 'Text' },
  { value: 'structured_data', label: 'Structured data (JSON-LD/meta)' },
]

function toggleFormat(formats: ScrapeFormat[], format: ScrapeFormat): ScrapeFormat[] {
  if (formats.includes(format)) {
    // At least one format must stay selected -- an empty list would ask the
    // backend for nothing and get nothing back.
    return formats.length === 1 ? formats : formats.filter((f) => f !== format)
  }
  return [...formats, format]
}

// Shared by the Scrape tab's own options and the Crawl tab's nested
// "scrape_options" block -- `ScrapeOptionsIn` is the same shape in both
// `ScrapeRequest` and `CrawlRequest.scrape_options` (the crawl variant just
// omits `tier`, which callers handle separately since it isn't part of this
// shared shape).
export function ScrapeOptionsFields({
  value,
  onChange,
  showAdvanced = true,
}: {
  value: ScrapeOptionsValue
  onChange: (next: ScrapeOptionsValue) => void
  showAdvanced?: boolean
}) {
  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-col gap-1.5">
        <Label>Formats</Label>
        <div className="flex flex-wrap gap-3">
          {FORMAT_OPTIONS.map((opt) => (
            <label key={opt.value} className="flex items-center gap-1.5 text-sm">
              <input
                type="checkbox"
                checked={value.formats.includes(opt.value)}
                onChange={() => onChange({ ...value, formats: toggleFormat(value.formats, opt.value) })}
              />
              {opt.label}
            </label>
          ))}
        </div>
      </div>

      <label className="flex items-center gap-2 text-sm">
        <input
          type="checkbox"
          checked={value.only_main_content}
          onChange={(e) => onChange({ ...value, only_main_content: e.target.checked })}
        />
        Only main content (strip nav/footer/boilerplate)
      </label>

      <label className="flex items-center gap-2 text-sm">
        <input
          type="checkbox"
          checked={value.screenshot}
          onChange={(e) => onChange({ ...value, screenshot: e.target.checked })}
        />
        Capture a screenshot
      </label>

      {showAdvanced && (
        <details className="text-sm text-muted-foreground">
          <summary className="cursor-pointer select-none">Advanced options</summary>
          <div className="mt-2 flex flex-col gap-3 text-left">
            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={value.full_page_screenshot}
                disabled={!value.screenshot}
                onChange={(e) => onChange({ ...value, full_page_screenshot: e.target.checked })}
              />
              Full page screenshot
            </label>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="timeout_ms">Timeout (ms)</Label>
              <Input
                id="timeout_ms"
                type="number"
                min={1}
                value={value.timeout_ms}
                onChange={(e) => onChange({ ...value, timeout_ms: Number(e.target.value) || 0 })}
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="wait_for_ms">Wait before extracting (ms)</Label>
              <Input
                id="wait_for_ms"
                type="number"
                min={0}
                value={value.wait_for_ms ?? ''}
                placeholder="none"
                onChange={(e) => onChange({ ...value, wait_for_ms: e.target.value ? Number(e.target.value) : null })}
              />
            </div>
            <StringListField
              label="Include tags (CSS selectors, comma-separated)"
              value={value.include_tags}
              onChange={(include_tags) => onChange({ ...value, include_tags })}
              placeholder="article, main, .content"
            />
            <StringListField
              label="Exclude tags (CSS selectors, comma-separated)"
              value={value.exclude_tags}
              onChange={(exclude_tags) => onChange({ ...value, exclude_tags })}
              placeholder="nav, footer, .ads"
            />

            <div className="flex flex-col gap-2 rounded-md border border-border p-3">
              <label className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={value.extract !== null}
                  onChange={(e) =>
                    onChange({ ...value, extract: e.target.checked ? { json_schema: null, prompt: '' } : null })
                  }
                />
                Enable LLM extraction
              </label>
              {value.extract !== null && (
                <div className="flex flex-col gap-3">
                  <p className="text-xs text-muted-foreground">
                    Calls an LLM over the page's markdown (server-side, gated by AGENTPILOT_LLM_API_KEY).
                    Provide a prompt, a JSON Schema to extract into, or both.
                  </p>
                  <div className="flex flex-col gap-1.5">
                    <Label htmlFor="extract-prompt">Prompt</Label>
                    <Textarea
                      id="extract-prompt"
                      rows={2}
                      placeholder="e.g. extract the product title and price"
                      value={value.extract.prompt ?? ''}
                      onChange={(e) =>
                        onChange({ ...value, extract: { ...(value.extract ?? {}), prompt: e.target.value || null } })
                      }
                    />
                  </div>
                  <JsonTextareaField
                    label="JSON Schema (optional)"
                    value={value.extract.json_schema ?? null}
                    onChange={(json_schema) =>
                      onChange({ ...value, extract: { ...(value.extract ?? {}), json_schema } })
                    }
                    placeholder={'{\n  "type": "object",\n  "properties": { "price": { "type": "number" } }\n}'}
                  />
                </div>
              )}
            </div>
          </div>
        </details>
      )}
    </div>
  )
}
