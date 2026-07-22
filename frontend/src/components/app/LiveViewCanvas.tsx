import { useEffect, useRef } from 'react'
import type { LiveViewMode } from '@/lib/api/liveView'

interface Props {
  frameUrl: string | null
  mode: LiveViewMode
  onInputEvent?: (event: Record<string, unknown>) => void
}

/** Renders the current screencast frame and, in `interact` mode, converts
 * client-space pointer/keyboard events into the natural-image-space
 * `InputEvent`s `routes/live_view.py` expects (same coordinate conversion
 * `app.js` used to do by hand). */
export function LiveViewCanvas({ frameUrl, mode, onInputEvent }: Props) {
  const imgRef = useRef<HTMLImageElement>(null)

  useEffect(() => {
    if (mode !== 'interact' || !onInputEvent) return
    const img = imgRef.current
    if (!img) return

    const toImageCoords = (e: MouseEvent) => {
      const rect = img.getBoundingClientRect()
      const scaleX = img.naturalWidth / rect.width
      const scaleY = img.naturalHeight / rect.height
      return { x: (e.clientX - rect.left) * scaleX, y: (e.clientY - rect.top) * scaleY }
    }

    const onMouseMove = (e: MouseEvent) => onInputEvent({ kind: 'mousemove', ...toImageCoords(e) })
    const onMouseDown = (e: MouseEvent) =>
      onInputEvent({ kind: 'mousedown', ...toImageCoords(e), button: 'left' })
    const onMouseUp = (e: MouseEvent) =>
      onInputEvent({ kind: 'mouseup', ...toImageCoords(e), button: 'left' })
    const onWheel = (e: WheelEvent) => {
      e.preventDefault()
      onInputEvent({ kind: 'wheel', ...toImageCoords(e), deltaX: e.deltaX, deltaY: e.deltaY })
    }
    const onKeyDown = (e: KeyboardEvent) => onInputEvent({ kind: 'keydown', key: e.key })
    const onKeyUp = (e: KeyboardEvent) => onInputEvent({ kind: 'keyup', key: e.key })

    img.addEventListener('mousemove', onMouseMove)
    img.addEventListener('mousedown', onMouseDown)
    img.addEventListener('mouseup', onMouseUp)
    img.addEventListener('wheel', onWheel, { passive: false })
    window.addEventListener('keydown', onKeyDown)
    window.addEventListener('keyup', onKeyUp)
    return () => {
      img.removeEventListener('mousemove', onMouseMove)
      img.removeEventListener('mousedown', onMouseDown)
      img.removeEventListener('mouseup', onMouseUp)
      img.removeEventListener('wheel', onWheel)
      window.removeEventListener('keydown', onKeyDown)
      window.removeEventListener('keyup', onKeyUp)
    }
  }, [mode, onInputEvent])

  return (
    <div className="flex h-full w-full items-center justify-center bg-black">
      {frameUrl ? (
        // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions
        <img
          ref={imgRef}
          src={frameUrl}
          alt="live view"
          className="max-h-full max-w-full select-none"
          draggable={false}
        />
      ) : (
        <p className="text-sm text-white/60">Connecting…</p>
      )}
    </div>
  )
}
