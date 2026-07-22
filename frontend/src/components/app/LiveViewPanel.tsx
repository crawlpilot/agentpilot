import { useEffect, useRef, useState, type FormEvent } from 'react'
import { AlertTriangle, ArrowLeft, ArrowRight, Eye, MousePointer2, RotateCw } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { useLiveView } from '@/hooks/useLiveView'
import { useExecuteSession } from '@/hooks/useExecuteSession'
import { LiveViewCanvas } from '@/components/app/LiveViewCanvas'
import { useToast } from '@/components/ui/toast'

const URL_POLL_MS = 2000

export function LiveViewPanel({ sessionId, toolbarEnd }: { sessionId: string; toolbarEnd?: React.ReactNode }) {
  const { mode, changeMode, status, frameUrl, sendInput } = useLiveView(sessionId, 'view')
  const [confirmInteract, setConfirmInteract] = useState(false)
  const hasConfirmedInteract = useRef(false)
  const execute = useExecuteSession()
  const { toast } = useToast()
  const [currentUrl, setCurrentUrl] = useState('')
  const [urlInput, setUrlInput] = useState('')
  const urlBarFocused = useRef(false)

  useEffect(() => {
    let cancelled = false
    function poll() {
      execute.mutate(
        { sessionId, actions: [{ type: 'execute_js', script: 'window.location.href' }] },
        {
          onSuccess: (result) => {
            if (cancelled) return
            const url = result.js_returns[0]
            if (typeof url === 'string') {
              setCurrentUrl(url)
              if (!urlBarFocused.current) setUrlInput(url)
            }
          },
        },
      )
    }
    poll()
    const id = setInterval(poll, URL_POLL_MS)
    return () => {
      cancelled = true
      clearInterval(id)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId])

  function requestInteract() {
    if (hasConfirmedInteract.current) {
      changeMode('interact')
    } else {
      setConfirmInteract(true)
    }
  }

  function runNav(actions: Parameters<typeof execute.mutate>[0]['actions']) {
    execute.mutate(
      { sessionId, actions },
      {
        onError: (err) => toast({ title: 'Navigation failed', description: err.message, variant: 'destructive' }),
      },
    )
  }

  function submitUrl(e: FormEvent) {
    e.preventDefault()
    if (!urlInput.trim()) return
    const url = /^[a-z][a-z0-9+.-]*:\/\//i.test(urlInput.trim()) ? urlInput.trim() : `https://${urlInput.trim()}`
    runNav([{ type: 'navigate', url }])
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

      <div className="flex items-center gap-1 border-b border-border px-2 py-1.5">
        <Button size="icon" variant="ghost" title="Back" onClick={() => runNav([{ type: 'go_back' }])}>
          <ArrowLeft className="size-4" />
        </Button>
        <Button
          size="icon"
          variant="ghost"
          title="Forward"
          onClick={() => runNav([{ type: 'execute_js', script: 'history.forward()' }])}
        >
          <ArrowRight className="size-4" />
        </Button>
        <Button
          size="icon"
          variant="ghost"
          title="Refresh"
          onClick={() => runNav([{ type: 'execute_js', script: 'location.reload()' }])}
        >
          <RotateCw className="size-4" />
        </Button>
        <form className="min-w-0 flex-1" onSubmit={submitUrl}>
          <Input
            value={urlInput}
            onChange={(e) => setUrlInput(e.target.value)}
            onFocus={() => (urlBarFocused.current = true)}
            onBlur={() => {
              urlBarFocused.current = false
              setUrlInput(currentUrl)
            }}
            placeholder="example.com"
            className="h-8 font-mono text-xs"
          />
        </form>
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
