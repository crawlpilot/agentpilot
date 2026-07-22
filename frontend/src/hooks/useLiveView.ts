import { useCallback, useEffect, useRef, useState } from 'react'
import { LiveViewSocket, type LiveViewMode, type LiveViewStatus } from '@/lib/api/liveView'
import { useAuth } from '@/lib/auth/AuthContext'

export function useLiveView(sessionId: string, initialMode: LiveViewMode = 'view', pageId?: string | null) {
  const { apiKey } = useAuth()
  const [mode, setMode] = useState<LiveViewMode>(initialMode)
  const [status, setStatus] = useState<LiveViewStatus>('connecting')
  const [frameUrl, setFrameUrl] = useState<string | null>(null)
  const socketRef = useRef<LiveViewSocket | null>(null)
  const currentUrlRef = useRef<string | null>(null)

  useEffect(() => {
    if (!apiKey) return
    const socket = new LiveViewSocket({
      sessionId,
      apiKey,
      mode: initialMode,
      pageId,
      onStatusChange: setStatus,
      onFrame: (blob) => {
        const url = URL.createObjectURL(blob)
        if (currentUrlRef.current) URL.revokeObjectURL(currentUrlRef.current)
        currentUrlRef.current = url
        setFrameUrl(url)
      },
    })
    socketRef.current = socket
    return () => {
      socket.close()
      if (currentUrlRef.current) URL.revokeObjectURL(currentUrlRef.current)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId, apiKey])

  useEffect(() => {
    socketRef.current?.setPageId(pageId)
  }, [pageId])

  const changeMode = useCallback((next: LiveViewMode) => {
    setMode(next)
    socketRef.current?.setMode(next)
  }, [])

  const sendInput = useCallback((event: Record<string, unknown>) => {
    socketRef.current?.sendInput(event)
  }, [])

  return { mode, changeMode, status, frameUrl, sendInput }
}
