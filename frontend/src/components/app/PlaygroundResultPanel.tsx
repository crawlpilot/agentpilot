import { useState } from 'react'
import { AlertTriangle, Check, Copy } from 'lucide-react'
import { SnapshotTree } from '@/components/app/SnapshotTree'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import type { ActionResult } from '@/lib/api/types'

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

export function PlaygroundResultPanel({ result }: { result: ActionResult }) {
  return (
    <div className="flex flex-col gap-4">
      {(result.sequence_aborted || result.page_changed) && (
        <div className="flex items-center gap-2 rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-sm text-warning">
          <AlertTriangle className="size-4 shrink-0" />
          {result.sequence_aborted && <span>Sequence stopped early — the page navigated mid-sequence.</span>}
          {result.page_changed && !result.sequence_aborted && <span>The page navigated during this sequence.</span>}
        </div>
      )}

      {result.screenshots.map((b64, i) => (
        <div key={i} className="overflow-hidden rounded-md border border-border">
          <img src={`data:image/png;base64,${b64}`} alt={`screenshot ${i + 1}`} className="w-full" />
        </div>
      ))}

      {result.extracts.map((text, i) => (
        <div key={i} className="rounded-md border border-border">
          <div className="flex items-center justify-between border-b border-border px-3 py-1.5">
            <span className="text-xs font-medium text-muted-foreground">Extract {i + 1}</span>
            <CopyButton text={text} />
          </div>
          <pre className="max-h-64 overflow-auto whitespace-pre-wrap p-3 text-xs">{text}</pre>
        </div>
      ))}

      {result.snapshots.map((snap, i) => (
        <div key={i} className="rounded-md border border-border">
          <div className="border-b border-border px-3 py-1.5 text-xs font-medium text-muted-foreground">
            Snapshot {i + 1} <Badge variant="outline">epoch {snap.epoch}</Badge>
          </div>
          <div className="max-h-64 overflow-auto p-2">
            <SnapshotTree node={snap.root} />
          </div>
        </div>
      ))}

      {result.js_returns.map((value, i) => (
        <div key={i} className="rounded-md border border-border">
          <div className="border-b border-border px-3 py-1.5 text-xs font-medium text-muted-foreground">
            execute_js return {i + 1}
          </div>
          <pre className="max-h-48 overflow-auto p-3 text-xs">{JSON.stringify(value, null, 2)}</pre>
        </div>
      ))}

      {result.downloads.map((d) => (
        <div key={d.artifact_id} className="rounded-md border border-border p-3 text-xs">
          <span className="font-medium">{d.kind}</span> — {d.size} bytes — {d.sha256.slice(0, 12)}…
        </div>
      ))}
    </div>
  )
}
