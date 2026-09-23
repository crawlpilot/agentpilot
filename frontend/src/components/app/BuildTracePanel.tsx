import { useState } from 'react'
import { ChevronDown, ChevronRight } from 'lucide-react'
import { Badge, type BadgeProps } from '@/components/ui/badge'
import { CopyButton } from '@/components/app/CopyButton'
import { useRunArtifacts } from '@/hooks/useRecipes'
import type { RecipeRunArtifact } from '@/lib/api/types'

/**
 * What the build tried, and what the model was asked and answered.
 *
 * A build that comes back with a wrong selector — or none — used to be
 * investigable only by running it again and watching the worker's stdout. The
 * verifier already writes its rejections to be read by a person ("the 10
 * matches are spread across the whole page — their nearest common ancestor is
 * <body>"); this is where they become visible.
 *
 * Two kinds, answering two different questions:
 *
 * - `trace` — every locator proposed and why each was turned down. Always
 *   stored: it is small, and the reasons are the diagnosis.
 * - `prompt` — one model call, its prompt and its reply. Stored only under
 *   `AGENTPILOT_RECIPE_TRACE_PROMPTS`, because a prompt carries the page
 *   snapshot and runs to tens of kilobytes. It answers the thing the attempts
 *   cannot: whether the model was asked the right question. "The selector is
 *   wrong" and "the section was never in what the model saw" look identical
 *   from the outside and need opposite fixes.
 */

interface Attempt {
  field: string
  stage: string
  outcome: string
  reason?: string
  read?: string
  transform?: string[]
  locator?: Record<string, unknown>
}

function outcomeVariant(outcome: string): NonNullable<BadgeProps['variant']> {
  if (outcome === 'bound') return 'success'
  if (outcome === 'rejected') return 'destructive'
  return 'outline'
}

function describeLocator(locator?: Record<string, unknown>): string {
  if (!locator) return ''
  const kind = String(locator.kind ?? '?')
  const target = (locator.selector as string) ?? (locator.path as string) ?? ''
  return `${kind}  ${target}`
}

function Collapsible({
  title,
  badge,
  children,
  defaultOpen = false,
}: {
  title: string
  badge?: React.ReactNode
  children: React.ReactNode
  defaultOpen?: boolean
}) {
  const [open, setOpen] = useState(defaultOpen)
  return (
    <div className="rounded-md border border-border">
      <button
        type="button"
        className="flex w-full items-center gap-2 px-2 py-1.5 text-left"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        {open ? (
          <ChevronDown className="size-3.5 shrink-0" />
        ) : (
          <ChevronRight className="size-3.5 shrink-0" />
        )}
        <span className="flex-1 truncate font-mono text-xs">{title}</span>
        {badge}
      </button>
      {open && <div className="border-t border-border p-2">{children}</div>}
    </div>
  )
}

/** Every locator tried for one field, and why each was turned down. */
function Attempts({ attempts }: { attempts: Attempt[] }) {
  const byField = new Map<string, Attempt[]>()
  for (const a of attempts) {
    const list = byField.get(a.field) ?? []
    list.push(a)
    byField.set(a.field, list)
  }

  return (
    <div className="flex flex-col gap-1">
      {[...byField.entries()].map(([field, list]) => {
        const bound = list.some((a) => a.outcome === 'bound')
        return (
          <Collapsible
            key={field || '(unnamed)'}
            title={field || '(unnamed)'}
            badge={
              <Badge variant={bound ? 'success' : 'destructive'}>
                {bound ? 'bound' : `${list.length} tried, none bound`}
              </Badge>
            }
          >
            <div className="flex flex-col gap-1.5">
              {list.map((a, i) => (
                <div key={i} className="flex flex-col gap-0.5">
                  <div className="flex flex-wrap items-center gap-1.5">
                    <Badge variant={outcomeVariant(a.outcome)}>{a.outcome}</Badge>
                    <Badge variant="outline">{a.stage}</Badge>
                    {a.transform && a.transform.length > 0 && (
                      <span className="text-[10px] text-muted-foreground">
                        {a.transform.join(' → ')}
                      </span>
                    )}
                  </div>
                  {a.locator && (
                    <code className="break-all font-mono text-[11px]">
                      {describeLocator(a.locator)}
                    </code>
                  )}
                  {/* Written to be read. Passed through rather than summarised. */}
                  {a.reason && (
                    <p className="text-[11px] text-muted-foreground">{a.reason}</p>
                  )}
                  {a.read && (
                    <code className="break-all font-mono text-[10px] text-muted-foreground">
                      read {a.read}
                    </code>
                  )}
                </div>
              ))}
            </div>
          </Collapsible>
        )
      })}
    </div>
  )
}

function Exchange({ body }: { body: Record<string, unknown> }) {
  const prompt = String(body.prompt ?? '')
  const response = String(body.response ?? '')
  const fields = (body.fields as string[] | undefined) ?? []
  return (
    <Collapsible
      title={`${String(body.stage ?? '?')} — ${fields.join(', ') || 'all fields'}`}
      badge={<Badge variant="outline">{prompt.length.toLocaleString()} chars</Badge>}
    >
      <div className="flex flex-col gap-2">
        <div>
          <div className="flex items-center justify-between">
            <span className="text-[11px] font-medium text-muted-foreground">
              What the model answered
            </span>
            <CopyButton text={response} />
          </div>
          <pre className="mt-1 max-h-72 overflow-auto whitespace-pre-wrap rounded border border-border p-2 text-[11px]">
            {response}
          </pre>
        </div>
        <div>
          <div className="flex items-center justify-between">
            <span className="text-[11px] font-medium text-muted-foreground">
              What it was shown
            </span>
            <CopyButton text={prompt} />
          </div>
          <pre className="mt-1 max-h-96 overflow-auto whitespace-pre-wrap rounded border border-border p-2 text-[10px] text-muted-foreground">
            {prompt}
          </pre>
        </div>
      </div>
    </Collapsible>
  )
}

export function BuildTracePanel({ recipeId, runId }: { recipeId: string; runId: string }) {
  const { data, isLoading } = useRunArtifacts(recipeId, runId)
  const artifacts: RecipeRunArtifact[] = data?.artifacts ?? []

  if (isLoading || artifacts.length === 0) return null

  const trace = artifacts.find((a) => a.kind === 'trace')
  const attempts = ((trace?.body?.attempts as Attempt[] | undefined) ?? []).filter(
    (a) => a && typeof a === 'object',
  )
  const prompts = artifacts.filter((a) => a.kind === 'prompt')
  const outline = artifacts.find((a) => a.kind === 'outline')

  return (
    <div className="flex flex-col gap-2">
      {attempts.length > 0 && (
        <div className="flex flex-col gap-1">
          <span className="text-xs text-muted-foreground">
            Every locator this build tried, and why each was turned down
          </span>
          <Attempts attempts={attempts} />
        </div>
      )}

      {prompts.length > 0 && (
        <div className="flex flex-col gap-1">
          <span className="text-xs text-muted-foreground">
            What the model was asked, and what it answered
          </span>
          {prompts.map((a, i) => (
            <Exchange key={i} body={a.body} />
          ))}
        </div>
      )}

      {outline && (
        <Collapsible
          title="The page JSON the model was shown"
          badge={<Badge variant="outline">outline</Badge>}
        >
          <pre className="max-h-96 overflow-auto whitespace-pre-wrap text-[10px]">
            {String(outline.body?.page_json ?? '')}
          </pre>
        </Collapsible>
      )}

      {prompts.length === 0 && attempts.length > 0 && (
        // Says why the interesting half is missing, rather than leaving someone
        // to wonder whether the build made any model calls at all.
        <p className="text-[11px] text-muted-foreground">
          Prompts and replies are not stored for this run. Set{' '}
          <code className="font-mono">AGENTPILOT_RECIPE_TRACE_PROMPTS=1</code> and
          rebuild to capture what the model was shown.
        </p>
      )}
    </div>
  )
}
