import { useState } from 'react'
import { Eye, EyeOff } from 'lucide-react'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Button } from '@/components/ui/button'
import { API_BASE_URL } from '@/lib/config'
import type { ActionIn } from '@/lib/api/types'

function maskKey(key: string): string {
  return key.length <= 12 ? '••••••••' : `${key.slice(0, 8)}${'•'.repeat(key.length - 12)}${key.slice(-4)}`
}

function buildCurl(host: string, sessionId: string | null, apiKey: string, actions: ActionIn[]): string {
  const body = JSON.stringify({ actions }, null, 2)
  if (!sessionId) {
    return `curl -X POST ${host}/v1/sessions \\\n  -H "Authorization: Bearer ${apiKey}" \\\n  -H "Content-Type: application/json" \\\n  -d '{"domain":"example.com","name":"my-session"}'`
  }
  return `curl -X POST ${host}/v1/sessions/${sessionId}/execute \\\n  -H "Authorization: Bearer ${apiKey}" \\\n  -H "Content-Type: application/json" \\\n  -d '${body}'`
}

function buildPython(host: string, sessionId: string | null, apiKey: string, actions: ActionIn[]): string {
  if (!sessionId) {
    return [
      'import httpx',
      '',
      `resp = httpx.post(`,
      `    "${host}/v1/sessions",`,
      `    headers={"Authorization": "Bearer ${apiKey}"},`,
      `    json={"domain": "example.com", "name": "my-session"},`,
      ')',
      'session_id = resp.json()["session_id"]',
    ].join('\n')
  }
  return [
    'import httpx',
    '',
    'resp = httpx.post(',
    `    "${host}/v1/sessions/${sessionId}/execute",`,
    `    headers={"Authorization": "Bearer ${apiKey}"},`,
    `    json={"actions": ${JSON.stringify(actions)}},`,
    ')',
  ].join('\n')
}

export function CodeSnippetPanel({
  sessionId,
  apiKey,
  actions,
}: {
  sessionId: string | null
  apiKey: string | null
  actions: ActionIn[]
}) {
  const [revealed, setRevealed] = useState(false)
  const host = API_BASE_URL
  const effectiveKey = apiKey ?? 'bk_live_...'
  const displayKey = revealed ? effectiveKey : maskKey(effectiveKey)

  return (
    <div className="rounded-md border border-border">
      <div className="flex items-center justify-between border-b border-border px-3 py-1.5">
        <span className="text-xs font-medium text-muted-foreground">Code snippet</span>
        <Button size="sm" variant="ghost" onClick={() => setRevealed((v) => !v)}>
          {revealed ? <EyeOff className="size-3.5" /> : <Eye className="size-3.5" />}
          {revealed ? 'Hide key' : 'Reveal key'}
        </Button>
      </div>
      <Tabs defaultValue="curl" className="p-2">
        <TabsList>
          <TabsTrigger value="curl">curl</TabsTrigger>
          <TabsTrigger value="python">Python</TabsTrigger>
        </TabsList>
        <TabsContent value="curl">
          <pre className="max-h-64 overflow-auto rounded-md bg-muted p-3 text-xs">
            {buildCurl(host, sessionId, displayKey, actions)}
          </pre>
        </TabsContent>
        <TabsContent value="python">
          <pre className="max-h-64 overflow-auto rounded-md bg-muted p-3 text-xs">
            {buildPython(host, sessionId, displayKey, actions)}
          </pre>
        </TabsContent>
      </Tabs>
    </div>
  )
}
