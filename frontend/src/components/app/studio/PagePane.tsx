import { useState } from 'react'
import { Plus, Search, Sparkles } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Badge } from '@/components/ui/badge'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { LiveViewPanel } from '@/components/app/LiveViewPanel'
import { EmptyState } from '@/components/app/EmptyState'
import { useSessionsList } from '@/hooks/useSessionsList'
import { useExecuteSession } from '@/hooks/useExecuteSession'
import { useToast } from '@/components/ui/toast'
import { PROBE_JS, findPaths, flattenProbe, hitToLocator, type PathHit, type ProbeResult } from '@/lib/recipe/probe'
import type { Locator } from '@/lib/recipe/types'

interface Props {
  selectedField: string | null
  onAddCandidate: (locator: Locator) => void
}

/**
 * The centre pane: the page itself, and what is already in its JSON.
 *
 * The picker here is inverted on purpose. A conventional element picker asks
 * "what selector reaches this element?" and answers in CSS, which is the
 * least durable of the seven locator kinds. This asks "where does this value
 * already live?", and on a page carrying JSON-LD or hydration state the
 * answer is usually a path that outlives the next redesign.
 */
export function PagePane({ selectedField, onAddCandidate }: Props) {
  const { data } = useSessionsList()
  const [sessionId, setSessionId] = useState<string | null>(null)
  const sessions = (data?.sessions ?? []).filter((s) => s.state === 'active')
  const active = sessionId ?? sessions[0]?.session_id ?? null

  if (sessions.length === 0) {
    return (
      <div className="flex h-full items-center justify-center p-6">
        <EmptyState
          title="No live session"
          description="Open a browser session to load a page here, read its structured data, and bind fields against something real."
          action={
            <Button variant="outline" onClick={() => window.open('/sessions', '_blank')}>
              Open a session
            </Button>
          }
        />
      </div>
    )
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex items-center gap-2 border-b border-border px-3 py-1.5">
        <Select value={active ?? ''} onValueChange={setSessionId}>
          <SelectTrigger className="h-7 w-64 text-xs">
            <SelectValue placeholder="pick a session" />
          </SelectTrigger>
          <SelectContent>
            {sessions.map((s) => (
              <SelectItem key={s.session_id} value={s.session_id}>
                {s.name || s.session_id}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <span className="text-[11px] text-muted-foreground">
          navigate to a sample URL, then read its JSON below
        </span>
      </div>

      <div className="min-h-0 flex-1">{active && <LiveViewPanel sessionId={active} />}</div>

      {active && (
        <StructuredDataProbe sessionId={active} selectedField={selectedField} onAddCandidate={onAddCandidate} />
      )}
    </div>
  )
}

function StructuredDataProbe({
  sessionId,
  selectedField,
  onAddCandidate,
}: {
  sessionId: string
  selectedField: string | null
  onAddCandidate: (locator: Locator) => void
}) {
  const execute = useExecuteSession()
  const { toast } = useToast()
  const [hits, setHits] = useState<PathHit[] | null>(null)
  const [counts, setCounts] = useState({ json_ld: 0, meta: 0, hydration: 0 })
  const [needle, setNeedle] = useState('')

  function probe() {
    execute.mutate(
      { sessionId, actions: [{ type: 'execute_js', script: PROBE_JS }] },
      {
        onSuccess: (result) => {
          const raw = result.js_returns[0] as ProbeResult | null
          if (!raw || typeof raw !== 'object') {
            toast({ title: 'Probe returned nothing', description: 'Is a page loaded in this session?' })
            return
          }
          const probeResult: ProbeResult = {
            json_ld: raw.json_ld ?? [],
            meta: raw.meta ?? {},
            hydration: raw.hydration ?? {},
          }
          setCounts({
            json_ld: probeResult.json_ld.length,
            meta: Object.keys(probeResult.meta).length,
            hydration: Object.keys(probeResult.hydration).length,
          })
          setHits(flattenProbe(probeResult))
        },
        onError: (err) => toast({ title: 'Probe failed', description: err.message, variant: 'destructive' }),
      },
    )
  }

  const matches = hits ? findPaths(hits, needle) : []

  return (
    <div className="flex max-h-[45%] shrink-0 flex-col border-t border-border">
      <div className="flex flex-wrap items-center gap-2 px-3 py-1.5">
        <Button size="sm" variant="outline" onClick={probe} disabled={execute.isPending}>
          <Sparkles className="size-3.5" />
          {execute.isPending ? 'Reading…' : hits ? 'Re-read page JSON' : 'Read page JSON'}
        </Button>
        {hits && (
          <>
            <Badge variant={counts.json_ld ? 'accent' : 'outline'}>{counts.json_ld} JSON-LD</Badge>
            <Badge variant={counts.hydration ? 'accent' : 'outline'}>{counts.hydration} hydration</Badge>
            <Badge variant="outline">{counts.meta} meta</Badge>
            <span className="text-[11px] text-muted-foreground">{hits.length} addressable values</span>
          </>
        )}
      </div>

      {hits && counts.json_ld + counts.hydration + counts.meta === 0 && (
        <p className="px-3 pb-2 text-[11px] text-muted-foreground">
          Nothing embedded. This page has to be read from the DOM -- css, xpath or ax_role candidates, and it will need
          more care when the site is redesigned.
        </p>
      )}

      {hits && hits.length > 0 && (
        <>
          <div className="flex items-center gap-2 px-3 pb-1.5">
            <Search className="size-3.5 shrink-0 text-muted-foreground" />
            <Input
              className="h-7 text-xs"
              placeholder="paste a value you can see on the page -- price, title, a spec value"
              value={needle}
              onChange={(e) => setNeedle(e.target.value)}
            />
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto px-3 pb-2">
            {needle.trim().length >= 2 && matches.length === 0 && (
              <p className="text-[11px] text-muted-foreground">
                Not in the page's JSON. That is a real answer: bind it from the DOM instead.
              </p>
            )}
            {matches.map((hit, i) => (
              <div key={i} className="flex items-center gap-2 border-b border-border/50 py-1 last:border-0">
                <Badge variant="accent" className="shrink-0">
                  {hit.kind}
                </Badge>
                <span className="min-w-0 flex-1 truncate font-mono text-[11px]" title={hit.path}>
                  {hit.path}
                </span>
                <span className="w-40 shrink-0 truncate text-[11px] text-muted-foreground" title={hit.value}>
                  {hit.value}
                </span>
                <Button
                  size="sm"
                  variant="ghost"
                  className="h-6 shrink-0 px-1.5 text-[11px]"
                  disabled={!selectedField}
                  title={selectedField ? `Add as a candidate for ${selectedField}` : 'Select a field first'}
                  onClick={() => onAddCandidate(hitToLocator(hit))}
                >
                  <Plus className="size-3" />
                  {selectedField ?? 'pick a field'}
                </Button>
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  )
}
