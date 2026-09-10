/**
 * `VisualElementPicker` — expanding the selection has to survive the cursor.
 *
 * This is one test for one bug, and the bug was invisible by inspection:
 * `handleExternalAction('ArrowUp')` sets `currentElement` to the parent and
 * redraws, which is exactly right. `handleMouseMove` then resets it to whatever
 * is under the cursor — and reaching the studio's own ↑ button means dragging
 * the cursor across the live view, every pixel of which is forwarded into the
 * page as a `mousemove` (`LiveViewCanvas.tsx`). So expansion did not half-work;
 * it was undone between the two actions the user has to perform, and Enter
 * committed the leaf.
 *
 * Upstream this could not happen: ↑ was a real keystroke and the cursor never
 * had to move. The divergence is deliberate and recorded in the vendored file.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { VisualElementPicker, setPickerSink } from './vendor/content/features/picker/VisualElementPicker'
import type { ISelectionStrategy } from './vendor/content/features/picker/strategies/ISelectionStrategy'

/** Records what it is asked to highlight and what is finally committed. */
class _Strategy implements ISelectionStrategy {
  hovered: HTMLElement[] = []
  committed: HTMLElement | null = null
  preferredAction: 'extract' | 'click' = 'extract'

  handleHover(element: HTMLElement): void {
    this.hovered.push(element)
  }

  handleClick(element: HTMLElement): void {
    this.committed = element
  }

  get last(): HTMLElement | undefined {
    return this.hovered[this.hovered.length - 1]
  }
}

function move(el: Element) {
  // `handleMouseMove` resolves the target with `elementFromPoint`, which jsdom
  // does not implement — so it is stubbed to answer with the element the test
  // means the cursor to be over.
  document.elementFromPoint = () => el
  document.dispatchEvent(new MouseEvent('mousemove', { clientX: 1, clientY: 1, bubbles: true }))
}

describe('VisualElementPicker selection pinning', () => {
  let picker: VisualElementPicker
  let strategy: _Strategy

  beforeEach(() => {
    document.body.innerHTML = `
      <section id="specs">
        <h2 id="heading">Specifications</h2>
        <table id="rows"><tr><th id="cell">Brand</th></tr></table>
      </section>
      <div id="elsewhere">something else</div>`
    setPickerSink(() => {})
    // jsdom lays nothing out, so every element reports a 0x0 box and
    // `hasRenderedBox` — which ArrowDown uses to skip invisible children —
    // rejects all of them. Give them a size so the traversal has something to
    // traverse.
    for (const el of document.querySelectorAll('*')) {
      Object.defineProperty(el, 'offsetHeight', { value: 20, configurable: true })
    }
    strategy = new _Strategy()
    picker = new VisualElementPicker()
    picker.activate(strategy, () => {})
    vi.useFakeTimers()
  })

  /** `handleMouseMove` defers its work to a rAF; flush it. */
  function flush() {
    vi.advanceTimersByTime(32)
  }

  it('holds the expanded selection while the cursor moves away', () => {
    const heading = document.getElementById('heading')!
    const section = document.getElementById('specs')!

    move(heading)
    flush()
    expect(picker.selection?.tag).toBe('h2')

    picker.handleExternalAction('ArrowUp')
    expect(strategy.last).toBe(section)

    // The journey to the panel's own ↑ button. Before pinning, this is what
    // silently put the selection back on the heading.
    move(document.getElementById('elsewhere')!)
    flush()

    expect(picker.selection?.text).toContain('Brand')
    picker.handleExternalAction('Enter')
    expect(strategy.committed).toBe(section)
  })

  it('follows the cursor again once unpinned', () => {
    const heading = document.getElementById('heading')!
    move(heading)
    flush()
    picker.handleExternalAction('ArrowUp')
    expect(picker.selection?.pinned).toBe(true)

    picker.handleExternalAction('Unpin')
    expect(picker.selection?.pinned).toBe(false)

    const elsewhere = document.getElementById('elsewhere')!
    move(elsewhere)
    flush()
    expect(strategy.last).toBe(elsewhere)
  })

  it('narrowing pins too, for the same reason', () => {
    move(document.getElementById('specs')!)
    flush()
    picker.handleExternalAction('ArrowDown')

    const narrowed = strategy.last
    move(document.getElementById('elsewhere')!)
    flush()

    expect(strategy.last).toBe(narrowed)
  })

  it('hover still works before anything is adjusted', () => {
    const heading = document.getElementById('heading')!
    move(heading)
    flush()
    expect(strategy.last).toBe(heading)

    const cell = document.getElementById('cell')!
    move(cell)
    flush()
    expect(strategy.last).toBe(cell)
  })

  it('a new pick starts unpinned', () => {
    move(document.getElementById('heading')!)
    flush()
    picker.handleExternalAction('ArrowUp')
    picker.deactivate()

    const next = new _Strategy()
    picker.activate(next, () => {})
    const elsewhere = document.getElementById('elsewhere')!
    move(elsewhere)
    flush()

    expect(next.last).toBe(elsewhere)
  })
})
