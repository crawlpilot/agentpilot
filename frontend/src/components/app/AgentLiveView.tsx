import { Radio } from 'lucide-react'
import { useLiveView } from '@/hooks/useLiveView'
import { LiveViewCanvas } from '@/components/app/LiveViewCanvas'

/**
 * A read-only live view of the browser an agent run is driving. Unlike
 * `LiveViewPanel`, it deliberately does NOT poll the session with `execute`
 * (list_tabs / location.href) -- the agent worker is driving the same context
 * concurrently, and issuing our own CDP calls against it would race the loop.
 * The screencast (CDP Page.startScreencast) is passive, so watching is safe.
 *
 * The run's session is named `agent-run-{runId}` by the worker
 * (agent_worker_loop.py) and only exists while the run is active, so mount
 * this only for a running run and expect a clean close once it finishes.
 */
export function AgentLiveView({ runId }: { runId: string }) {
  const sessionId = `agent-run-${runId}`
  const { status, frameUrl } = useLiveView(sessionId, 'view', null)

  return (
    <div className="overflow-hidden rounded-lg border border-border bg-card shadow-sm">
      <div className="flex items-center gap-2 border-b border-border bg-muted/40 px-3 py-2">
        <span className="flex items-center gap-1.5 text-xs font-medium">
          <Radio className={status === 'open' ? 'size-3.5 text-destructive' : 'size-3.5 text-muted-foreground'} />
          Live browser
        </span>
        <span className="ml-auto text-[11px] capitalize text-muted-foreground">{statusLabel(status)}</span>
      </div>
      <div className="aspect-[16/10] w-full bg-black">
        {status === 'closed' || status === 'error' ? (
          <div className="flex h-full items-center justify-center px-4 text-center text-xs text-white/60">
            {status === 'error'
              ? 'Live view unavailable for this run.'
              : 'The session has closed — the run is no longer live.'}
          </div>
        ) : (
          <LiveViewCanvas frameUrl={frameUrl} mode="view" />
        )}
      </div>
      <div className="flex items-center gap-1.5 border-t border-border bg-muted/40 px-3 py-1.5 text-[11px] text-muted-foreground">
        <span>Watching the agent — screencast over WebSocket</span>
      </div>
    </div>
  )
}

function statusLabel(status: string): string {
  switch (status) {
    case 'open':
      return 'live'
    case 'connecting':
      return 'connecting…'
    case 'reconnecting':
      return 'reconnecting…'
    case 'closed':
      return 'ended'
    default:
      return status
  }
}
