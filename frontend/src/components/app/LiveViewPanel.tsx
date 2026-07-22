import { useEffect, useRef, useState, type FormEvent } from 'react'
import { AlertTriangle, ArrowLeft, ArrowRight, Eye, MousePointer2, Plus, RotateCw, X } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { useLiveView } from '@/hooks/useLiveView'
import { useExecuteSession } from '@/hooks/useExecuteSession'
import { LiveViewCanvas } from '@/components/app/LiveViewCanvas'
import { useToast } from '@/components/ui/toast'
import { cn } from '@/lib/utils'
import type { TabInfo } from '@/lib/api/types'

const POLL_MS = 2000

export function LiveViewPanel({ sessionId, toolbarEnd }: { sessionId: string; toolbarEnd?: React.ReactNode }) {
  // `activePageId` is `null` until the first poll resolves it from the
  // backend's own `cctx.active_page_id` (see `_list_tabs` in
  // patchright_driver.py) -- undefined/null both mean "the session's
  // current active tab" to `execute()`/live-view, so this is safe to pass
  // straight through before that first poll lands.
  const [activePageId, setActivePageId] = useState<string | null>(null)
  const [tabs, setTabs] = useState<TabInfo[]>([])
  const { mode, changeMode, status, frameUrl, sendInput } = useLiveView(sessionId, 'view', activePageId)
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
        {
          sessionId,
          actions: [{ type: 'list_tabs' }, { type: 'execute_js', script: 'window.location.href' }],
          pageId: activePageId,
        },
        {
          onSuccess: (result) => {
            if (cancelled) return
            const tabList = result.tabs[0]
            if (tabList) {
              setTabs(tabList)
              // Always follow the backend's idea of the active tab, not just
              // on first load -- this is what makes the live view auto-jump
              // to a new tab the *page itself* opens (a real popup, per the
              // multi-tab plan's confirmed auto-focus semantics), not only
              // ones opened through this UI's own "+" button.
              const active = tabList.find((t) => t.active)
              if (active) setActivePageId(active.page_id)
            }
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
    const id = setInterval(poll, POLL_MS)
    return () => {
      cancelled = true
      clearInterval(id)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId, activePageId])

  function requestInteract() {
    if (hasConfirmedInteract.current) {
      changeMode('interact')
    } else {
      setConfirmInteract(true)
    }
  }

  function runNav(actions: Parameters<typeof execute.mutate>[0]['actions']) {
    execute.mutate(
      { sessionId, actions, pageId: activePageId },
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

  function switchTab(pageId: string) {
    if (pageId === activePageId) return
    setActivePageId(pageId)
    execute.mutate(
      { sessionId, actions: [{ type: 'switch_tab', page_id: pageId }] },
      {
        onError: (err) => toast({ title: 'Switch tab failed', description: err.message, variant: 'destructive' }),
      },
    )
  }

  function newTab() {
    execute.mutate(
      { sessionId, actions: [{ type: 'new_tab' }] },
      {
        // The new tab's id isn't in the response -- ActionResult has no
        // per-action id to correlate against (see spi.actions' NewTabAction
        // docstring); the next poll (<=2s) picks it up as the backend's new
        // active tab and syncs `activePageId` from there instead.
        onSuccess: () => setActivePageId(null),
        onError: (err) => toast({ title: 'New tab failed', description: err.message, variant: 'destructive' }),
      },
    )
  }

  function closeTab(pageId: string) {
    execute.mutate(
      { sessionId, actions: [{ type: 'close_tab', page_id: pageId }] },
      {
        onError: (err) => toast({ title: 'Close tab failed', description: err.message, variant: 'destructive' }),
      },
    )
    if (pageId === activePageId) setActivePageId(null)
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

      {tabs.length > 0 && (
        <div className="flex items-center gap-1 overflow-x-auto border-b border-border bg-muted/30 px-2 py-1">
          {tabs.map((t) => (
            <div
              key={t.page_id}
              onClick={() => switchTab(t.page_id)}
              className={cn(
                'flex max-w-48 shrink-0 cursor-pointer items-center gap-1.5 rounded-t-md border border-b-0 border-transparent px-2 py-1 text-xs',
                t.page_id === activePageId
                  ? 'border-border bg-background text-foreground'
                  : 'text-muted-foreground hover:bg-muted',
              )}
            >
              <span className="truncate">{t.title || t.url || 'New tab'}</span>
              {tabs.length > 1 && (
                <button
                  onClick={(e) => {
                    e.stopPropagation()
                    closeTab(t.page_id)
                  }}
                  className="shrink-0 rounded hover:bg-muted-foreground/20"
                  title="Close tab"
                >
                  <X className="size-3" />
                </button>
              )}
            </div>
          ))}
          <Button size="icon" variant="ghost" className="h-6 w-6 shrink-0" title="New tab" onClick={newTab}>
            <Plus className="size-3.5" />
          </Button>
        </div>
      )}

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
