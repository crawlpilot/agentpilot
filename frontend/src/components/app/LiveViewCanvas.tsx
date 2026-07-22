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
    // Keyboard listeners are on `window` (there's no way to focus the `img`
    // element itself), so anything else focusable on the page -- the live
    // view's own URL bar, a dialog input, etc. -- would otherwise leak every
    // keystroke into the remote page too, on top of the local field doing
    // its own thing with it. Only forward when nothing editable has focus.
    const isEditableTarget = (target: EventTarget | null) =>
      target instanceof HTMLElement &&
      (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.isContentEditable)

    const onKeyDown = (e: KeyboardEvent) => {
      if (isEditableTarget(e.target)) return
      onInputEvent({ kind: 'keydown', key: e.key })
    }
    const onKeyUp = (e: KeyboardEvent) => {
      if (isEditableTarget(e.target)) return
      onInputEvent({ kind: 'keyup', key: e.key })
    }

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
