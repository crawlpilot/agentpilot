// Wraps the raw WebSocket in `routes/live_view.py`'s protocol: binary
// screencast frames out, JSON `InputEvent`s in (interact mode only).
// Browsers can't set custom headers on a WS handshake, so the api key
// travels as `?api_key=...` -- same tradeoff as the backend route itself.

export type LiveViewMode = 'view' | 'interact'
export type LiveViewStatus = 'connecting' | 'open' | 'reconnecting' | 'closed' | 'error'

// Codes the backend closes with deliberately (invalid session/key/capability)
// -- retrying on these would just spin forever against the same rejection.
const _TERMINAL_CLOSE_CODES = new Set([4401, 4404, 4501, 4502])

interface LiveViewSocketOptions {
  sessionId: string
  apiKey: string
  mode: LiveViewMode
  pageId?: string | null
  onFrame: (blob: Blob) => void
  onStatusChange: (status: LiveViewStatus) => void
}

export class LiveViewSocket {
  private readonly opts: LiveViewSocketOptions
  private ws: WebSocket | null = null
  private mode: LiveViewMode
  private pageId: string | null | undefined
  private deliberateClose = false
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null

  constructor(opts: LiveViewSocketOptions) {
    this.opts = opts
    this.mode = opts.mode
    this.pageId = opts.pageId
    this.connect()
  }

  private connect(): void {
    this.opts.onStatusChange('connecting')
    const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
    const pageParam = this.pageId ? `&page_id=${encodeURIComponent(this.pageId)}` : ''
    const url =
      `${proto}://${window.location.host}/v1/sessions/${this.opts.sessionId}/live-view` +
      `?mode=${this.mode}&api_key=${encodeURIComponent(this.opts.apiKey)}${pageParam}`

    const ws = new WebSocket(url)
    ws.binaryType = 'blob'
    ws.onopen = () => this.opts.onStatusChange('open')
    ws.onmessage = (ev) => {
      if (ev.data instanceof Blob) this.opts.onFrame(ev.data)
    }
    ws.onclose = (ev) => {
      if (this.deliberateClose) {
        this.opts.onStatusChange('closed')
        return
      }
      if (_TERMINAL_CLOSE_CODES.has(ev.code)) {
        this.opts.onStatusChange('error')
        return
      }
      this.opts.onStatusChange('reconnecting')
      this.reconnectTimer = setTimeout(() => this.connect(), 1000)
    }
    this.ws = ws
  }

  /** Backend can't switch mode on an open socket yet -- this transparently
   * closes and reopens with the new `?mode=`, surfaced to the UI only as a
   * brief "reconnecting" status, never as a manual disconnect action. */
  setMode(mode: LiveViewMode): void {
    if (this.mode === mode) return
    this.mode = mode
    this.ws?.close()
  }

  /** Same transparent reconnect-on-change as `setMode` -- switching the
   * active tab means a different page's screencast, which this protocol
   * can't retarget on an already-open socket. */
  setPageId(pageId: string | null | undefined): void {
    if (this.pageId === pageId) return
    this.pageId = pageId
    this.ws?.close()
  }

  sendInput(event: Record<string, unknown>): void {
    if (this.mode === 'interact' && this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(event))
    }
  }

  close(): void {
    this.deliberateClose = true
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer)
    this.ws?.close()
  }
}
