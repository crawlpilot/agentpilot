import { useRef, useState } from 'react'
import { AlertTriangle, Eye, MousePointer2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { useLiveView } from '@/hooks/useLiveView'
import { LiveViewCanvas } from '@/components/app/LiveViewCanvas'

export function LiveViewPanel({ sessionId, toolbarEnd }: { sessionId: string; toolbarEnd?: React.ReactNode }) {
  const { mode, changeMode, status, frameUrl, sendInput } = useLiveView(sessionId, 'view')
  const [confirmInteract, setConfirmInteract] = useState(false)
  const hasConfirmedInteract = useRef(false)

  function requestInteract() {
    if (hasConfirmedInteract.current) {
      changeMode('interact')
    } else {
      setConfirmInteract(true)
    }
  }

  return (
    <div className="relative flex h-full min-h-0 flex-col">
      <div className="flex items-center justify-between border-b border-border px-4 py-2">
        <div className="flex items-center gap-2">
          {mode === 'interact' ? (
            <span className="flex items-center gap-1.5 text-sm font-semibold text-destructive">
              <span className="size-2 animate-pulse rounded-full bg-destructive" />
              INTERACT -- you are in control
            </span>
          ) : (
            <span className="text-sm font-medium text-muted-foreground">Live view</span>
          )}
          <span className="text-xs text-muted-foreground">({status})</span>
        </div>
        <div className="flex items-center gap-2">
          <Button size="sm" variant={mode === 'view' ? 'secondary' : 'ghost'} onClick={() => changeMode('view')}>
            <Eye className="size-4" />
            View
          </Button>
          <Button
            size="sm"
            variant={mode === 'interact' ? 'destructive' : 'outline'}
            onClick={requestInteract}
          >
            <MousePointer2 className="size-4" />
            Interact
          </Button>
          {toolbarEnd}
        </div>
      </div>

      <div className="min-h-0 flex-1">
        <LiveViewCanvas frameUrl={frameUrl} mode={mode} onInputEvent={sendInput} />
      </div>

      {confirmInteract && (
        <div className="absolute inset-0 flex items-center justify-center bg-black/70 p-6">
          <div className="max-w-sm rounded-lg border border-border bg-card p-6 text-center">
            <AlertTriangle className="mx-auto mb-3 size-8 text-destructive" />
            <p className="mb-1 font-semibold">Take manual control?</p>
            <p className="mb-4 text-sm text-muted-foreground">
              You are about to take manual control of a live automated session. Any clicks or
              keystrokes you send will run for real, against the real page.
            </p>
            <div className="flex justify-center gap-2">
              <Button variant="outline" onClick={() => setConfirmInteract(false)}>
                Cancel
              </Button>
              <Button
                variant="destructive"
                onClick={() => {
                  hasConfirmedInteract.current = true
                  setConfirmInteract(false)
                  changeMode('interact')
                }}
              >
                Take control
              </Button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
