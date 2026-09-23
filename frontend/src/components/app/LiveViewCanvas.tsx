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

    // The `<img>` box now always fills its container (`object-contain`
    // sizing, see the render below) rather than shrinking to exactly match
    // the frame's own pixels, so unlike before, the element's bounding rect
    // and the actual visible (letterboxed) image content can now differ --
    // the click math has to account for `object-contain`'s own centering
    // and scale-to-fit, not just naively rescale against the outer box.
    /**
     * Client-space pointer position in natural-image space, or `null` when the
     * geometry cannot answer that yet.
     *
     * **`null` is not a formality.** Until the first screencast frame has
     * decoded, `naturalWidth`/`naturalHeight` are 0, so `scale` is `Infinity`,
     * `naturalWidth * scale` is `NaN`, and every coordinate below comes out
     * `NaN`. `JSON.stringify({x: NaN})` does not fail — it emits `{"x": null}`
     * — so the websocket cheerfully sent `{"x": null, "y": null}` and Chrome
     * answered `Input.dispatchMouseEvent: Invalid parameters`, which propagated
     * out of the route handler and tore down the live-view socket. The symptom
     * was simply that clicking in the live view did nothing.
     *
     * A zero-width rect (the panel collapsed or still laying out) divides the
     * other way and produces `Infinity`, which serialises to `null` just the
     * same. Hence the finite check rather than a check for either cause.
     */
    const toImageCoords = (e: MouseEvent) => {
      const rect = img.getBoundingClientRect()
      if (!img.naturalWidth || !img.naturalHeight || !rect.width || !rect.height) return null
      const scale = Math.min(rect.width / img.naturalWidth, rect.height / img.naturalHeight)
      const offsetX = (rect.width - img.naturalWidth * scale) / 2
      const offsetY = (rect.height - img.naturalHeight * scale) / 2
      const x = (e.clientX - rect.left - offsetX) / scale
      const y = (e.clientY - rect.top - offsetY) / scale
      return Number.isFinite(x) && Number.isFinite(y) ? { x, y } : null
    }

    const onMouseMove = (e: MouseEvent) => {
      const at = toImageCoords(e)
      if (at) onInputEvent({ kind: 'mousemove', ...at })
    }
    const onMouseDown = (e: MouseEvent) => {
      const at = toImageCoords(e)
      if (at) onInputEvent({ kind: 'mousedown', ...at, button: 'left' })
    }
    const onMouseUp = (e: MouseEvent) => {
      const at = toImageCoords(e)
      if (at) onInputEvent({ kind: 'mouseup', ...at, button: 'left' })
    }
    const onWheel = (e: WheelEvent) => {
      e.preventDefault()
      const at = toImageCoords(e)
      if (at) onInputEvent({ kind: 'wheel', ...at, deltaX: e.deltaX, deltaY: e.deltaY })
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
          // `object-contain`, not `max-h-full max-w-full`: `max-*` only ever
          // shrinks an oversized image, it never scales a *smaller* one up
          // to fill the box -- the screencast frame's natural resolution can
          // easily be smaller than the viewport, which is exactly what left
          // it floating small inside the black letterbox instead of filling
          // the screen. `h-full w-full object-contain` always fills the
          // container in both directions, still without cropping/distorting.
          className="h-full w-full select-none object-contain"
          draggable={false}
        />
      ) : (
        <p className="text-sm text-white/60">Connecting…</p>
      )}
    </div>
  )
}
