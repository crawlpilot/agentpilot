/**
 * Handles the visual selection UI (overlay, tooltip, highlighting)
 *
 * VENDORED from crawlPilot @ a9355a8 (release-2.0.0),
 * `src/content/features/picker/VisualElementPicker.ts`. See
 * `frontend/src/lib/picker/README.md` before editing -- keep this in sync
 * with upstream rather than diverging.
 *
 * The one intentional divergence: upstream delivered a finished selection by
 * calling `chrome.runtime.sendMessage(msg)` in four places. There is no
 * extension runtime here -- the picker is injected into a remote page over
 * CDP and read back out through `execute_js` -- so those four calls are
 * replaced by the single `emit` sink below, which `entry.ts` points at
 * `window.__cpPickResult`. Everything else is upstream, verbatim.
 */
import type { ISelectionStrategy } from './strategies/ISelectionStrategy';
import { hasRenderedBox, getVisualRect } from '../../utils/rendering';

/** Where a completed selection goes. Replaced at install time by `entry.ts`. */
let emit: (msg: unknown) => void = () => {};

export function setPickerSink(sink: (msg: unknown) => void) {
    emit = sink;
}

export class VisualElementPicker {
    private overlay: HTMLElement | null = null;
    private tooltip: HTMLElement | null = null;
    private shortcutBar: HTMLElement | null = null;
    private highlightBox: HTMLElement | null = null;
    private siblingHighlights: Array<{ target: HTMLElement; box: HTMLElement }> = []; // Dashed boxes + their tracked elements
    private currentElement: HTMLElement | null = null;
    private isActive: boolean = false;
    private strategy: ISelectionStrategy | null = null;
    private onCancelCallback: (() => void) | null = null;
    private lastHighlightedElement: HTMLElement | null = null;
    private lastSiblings: HTMLElement[] = [];
    private rafPending = false;
    /**
     * Set once the user has adjusted the selection with ↑/↓, which stops hover
     * from taking it back.
     *
     * DIVERGENCE from upstream, and the reason for it: this picker is driven
     * from a panel *outside* the page. `handleMouseMove` resets
     * `currentElement` to whatever is under the cursor on every mouse movement,
     * and `LiveViewCanvas` forwards every movement over the live view into the
     * page -- so travelling from the element to the studio's own ↑ button
     * overwrote the expansion before the click on it landed, and Enter then
     * committed the leaf. Expansion did not half-work; it was undone between
     * the two actions the user has to perform. In the extension, ↑ was a real
     * keystroke and the cursor never had to move, so the bug could not arise.
     */
    private pinned = false;

    constructor() {
        this.handleMouseMove = this.handleMouseMove.bind(this);
        this.handleClick = this.handleClick.bind(this);
        this.handleKeyDown = this.handleKeyDown.bind(this);
        this.handleScroll = this.handleScroll.bind(this);
    }

    public activate(
        strategy: ISelectionStrategy,
        onCancel: () => void
    ) {
        if (this.isActive) return;
        this.isActive = true;
        this.strategy = strategy;
        this.onCancelCallback = onCancel;

        this.createUI();
        this.attachEventListeners();
        console.log('✅ VisualElementPicker activated with strategy');
    }

    public deactivate() {
        if (!this.isActive) return;
        this.isActive = false;
        this.strategy = null;
        this.removeUI();
        this.removeEventListeners();
        this.currentElement = null;
        this.pinned = false;
        console.log('❌ VisualElementPicker deactivated');
    }

    public handleExternalAction(action: string) {
        if (action === 'Unpin') {
            this.pinned = false;
            return;
        }
        if (!this.isActive || !this.currentElement) return;

        if (action === 'ArrowUp') {
            const parent = this.currentElement.parentElement;
            if (parent && parent !== document.body && parent !== document.documentElement) {
                this.currentElement = parent;
                // Pin BEFORE redrawing: the user is about to move the cursor
                // back to the panel, and every pixel of that journey is a
                // forwarded mousemove that would otherwise undo this.
                this.pinned = true;
                if (this.strategy) this.strategy.handleHover(this.currentElement, this);
            }
        }

        if (action === 'ArrowDown') {
            // Navigate into the first meaningful child element
            const children = Array.from(this.currentElement.children) as HTMLElement[];
            const firstMeaningful = children.find(c =>
                hasRenderedBox(c) && (c.innerText || c.textContent || '').trim().length > 0
            );
            if (firstMeaningful) {
                this.currentElement = firstMeaningful;
                this.pinned = true;
                if (this.strategy) this.strategy.handleHover(this.currentElement, this);
            }
        }

        if (action === 'Enter') {
            if (this.strategy) {
                this.strategy.handleClick(this.currentElement, this, (msg) => emit(msg));
            }
        }
    }

    /** What the selection is currently on, for the panel to describe. */
    public get selection(): { tag: string; text: string; pinned: boolean } | null {
        if (!this.currentElement) return null;
        return {
            tag: this.currentElement.tagName.toLowerCase(),
            text: (this.currentElement.innerText || this.currentElement.textContent || '')
                .trim()
                .slice(0, 120),
            pinned: this.pinned,
        };
    }

    private createUI() {
        // 1. Overlay
        this.overlay = document.createElement('div');
        this.overlay.setAttribute('data-testid', 'element-picker-overlay');
        this.overlay.style.cssText = `
            position: fixed !important;
            top: 0 !important;
            left: 0 !important;
            width: 100vw !important;
            height: 100vh !important;
            z-index: 2147483645 !important;
            background-color: rgba(0, 0, 0, 0.2) !important;
            cursor: crosshair !important;
            pointer-events: all !important;
            display: block !important;
        `;

        // 2. Highlight Box (Main)
        this.highlightBox = document.createElement('div');
        this.highlightBox.setAttribute('data-testid', 'element-picker-highlight');
        this.highlightBox.style.cssText = `
            position: fixed !important;
            z-index: 2147483646 !important;
            pointer-events: none !important;
            border: 2px solid #22D3EE !important;
            background-color: rgba(34, 211, 238, 0.1) !important;
            box-shadow: 0 0 0 4000px rgba(0, 0, 0, 0.3), 0 0 10px rgba(34, 211, 238, 0.7) !important;
            transition: all 0.1s ease-out !important;
            display: none !important;
            border-radius: 4px !important;
        `;

        // 3. Tooltip
        this.tooltip = document.createElement('div');
        this.tooltip.setAttribute('data-testid', 'element-picker-tooltip');
        this.tooltip.style.cssText = `
            position: fixed !important;
            z-index: 2147483647 !important;
            padding: 8px 12px !important;
            background-color: #22D3EE !important;
            color: black !important;
            border-radius: 6px !important;
            font-weight: bold !important;
            font-size: 13px !important;
            font-family: system-ui, -apple-system, sans-serif !important;
            box-shadow: 0 4px 12px rgba(0, 0, 0, 0.3) !important;
            pointer-events: none !important;
            display: none !important;
            max-width: 300px !important;
            white-space: nowrap !important;
        `;

        // 4. Keyboard shortcut bar (fixed bottom-center, always visible)
        this.shortcutBar = document.createElement('div');
        this.shortcutBar.style.cssText = `
            position: fixed !important;
            bottom: 16px !important;
            left: 50% !important;
            transform: translateX(-50%) !important;
            z-index: 2147483647 !important;
            background: rgba(17, 24, 39, 0.92) !important;
            color: #e5e7eb !important;
            border: 1px solid rgba(255,255,255,0.12) !important;
            border-radius: 8px !important;
            padding: 8px 14px !important;
            font-family: system-ui, -apple-system, sans-serif !important;
            font-size: 11px !important;
            display: flex !important;
            gap: 16px !important;
            align-items: center !important;
            pointer-events: none !important;
            backdrop-filter: blur(4px) !important;
            box-shadow: 0 4px 16px rgba(0,0,0,0.4) !important;
            white-space: nowrap !important;
        `;
        this.shortcutBar.innerHTML = `
            <span style="display:flex;align-items:center;gap:5px;">
                <kbd style="background:rgba(255,255,255,0.15);border-radius:3px;padding:1px 5px;font-weight:600;font-family:inherit;">↑</kbd>
                <span style="opacity:0.75;">Expand</span>
            </span>
            <span style="display:flex;align-items:center;gap:5px;">
                <kbd style="background:rgba(255,255,255,0.15);border-radius:3px;padding:1px 5px;font-weight:600;font-family:inherit;">↓</kbd>
                <span style="opacity:0.75;">Narrow</span>
            </span>
            <span style="display:flex;align-items:center;gap:5px;">
                <kbd style="background:rgba(255,255,255,0.15);border-radius:3px;padding:1px 5px;font-weight:600;font-family:inherit;">Enter</kbd>
                <span style="opacity:0.75;">Select</span>
            </span>
            <span style="display:flex;align-items:center;gap:5px;">
                <kbd style="background:rgba(255,255,255,0.15);border-radius:3px;padding:1px 5px;font-weight:600;font-family:inherit;">Esc</kbd>
                <span style="opacity:0.75;">Cancel</span>
            </span>
        `;

        document.documentElement.appendChild(this.overlay);
        document.documentElement.appendChild(this.highlightBox);
        document.documentElement.appendChild(this.tooltip);
        document.documentElement.appendChild(this.shortcutBar);
    }

    private removeUI() {
        this.overlay?.remove();
        this.highlightBox?.remove();
        this.tooltip?.remove();
        this.shortcutBar?.remove();
        this.clearSiblingHighlights();
        this.overlay = null;
        this.highlightBox = null;
        this.tooltip = null;
        this.shortcutBar = null;
    }

    private clearSiblingHighlights() {
        this.siblingHighlights.forEach(({ box }) => box.remove());
        this.siblingHighlights = [];
    }

    private attachEventListeners() {
        document.addEventListener('mousemove', this.handleMouseMove, true);
        document.addEventListener('click', this.handleClick, true);
        document.addEventListener('keydown', this.handleKeyDown, true);
        document.addEventListener('scroll', this.handleScroll, { passive: true, capture: true });
    }

    private removeEventListeners() {
        document.removeEventListener('mousemove', this.handleMouseMove, true);
        document.removeEventListener('click', this.handleClick, true);
        document.removeEventListener('keydown', this.handleKeyDown, true);
        document.removeEventListener('scroll', this.handleScroll, true);
    }

    private handleMouseMove(e: MouseEvent) {
        // The user has adjusted the selection deliberately; a cursor that
        // happens to pass over the page must not undo that. See `pinned`.
        if (this.pinned) return;
        if (this.rafPending || !this.overlay || !this.strategy) return;
        this.rafPending = true;

        requestAnimationFrame(() => {
            this.rafPending = false;
            if (!this.overlay || !this.strategy) return;

            this.overlay.style.setProperty('pointer-events', 'none', 'important');
            const target = document.elementFromPoint(e.clientX, e.clientY) as HTMLElement;
            this.overlay.style.setProperty('pointer-events', 'all', 'important');

            if (!target || target === this.overlay || target === document.body || target === document.documentElement) return;

            if (this.currentElement !== target) {
                this.currentElement = target;
                this.strategy.handleHover(target, this);
            }
        });
    }

    public updateHighlightManual(
        element: HTMLElement,
        label: string,
        subLabel: string,
        anchorElement?: HTMLElement | null,
        siblings?: HTMLElement[]
    ) {
        if (!this.highlightBox || !this.tooltip) return;

        // Optimized check: Don't re-create highlight boxes if the container hasn't changed
        // This prevents flickering while moving the cursor WITHIN a card/container
        const siblingsChanged = siblings ? (
            siblings.length !== this.lastSiblings.length ||
            siblings.some((s, i) => s !== this.lastSiblings[i])
        ) : (this.lastSiblings.length > 0);

        // CRITICAL DEBOUNCE FIX:
        // Check if the actual "primary highlighted element" (the container/box) has changed.
        // Previously we checked 'lastHighlightedElement', but that might refer to the previous *hover* target.
        // We need to ensure that if the 'element' (container) passed in changes, we update.
        if (this.lastHighlightedElement === element && !siblingsChanged) {
            // BUT we still want the tooltip to follow the cursor (anchorElement)
            const anchorRect = anchorElement ? anchorElement.getBoundingClientRect() : element.getBoundingClientRect();
            this.positionTooltip(anchorRect);
            return;
        }

        this.lastHighlightedElement = element;
        this.lastSiblings = siblings || [];

        // DO NOT update currentElement here. 
        // currentElement tracks the "Hovered" element (the trigger),
        // whereas 'element' here is the "Highlighted" element (the result).
        // Updating it causes an infinite loop in handleMouseMove and breaks click targeting.
        // this.currentElement = element;

        // getVisualRect: display:contents items (LinkedIn cards) have a 0x0
        // bounding rect of their own — union their children so boxes stay visible
        const rect = getVisualRect(element);
        const anchorRect = anchorElement ? getVisualRect(anchorElement) : rect;

        // Update highlight box
        this.highlightBox.style.display = 'block';
        this.highlightBox.style.top = `${rect.top}px`;
        this.highlightBox.style.left = `${rect.left}px`;
        this.highlightBox.style.width = `${rect.width}px`;
        this.highlightBox.style.height = `${rect.height}px`;

        // Update Sibling Highlights
        this.clearSiblingHighlights();
        if (siblings && siblings.length > 0) {
            siblings.forEach(sibling => {
                // Don't highlight the container itself if it's in the list (unlikely)
                if (sibling === element) return;

                const sRect = getVisualRect(sibling);
                if (sRect.width === 0 && sRect.height === 0) return;
                const box = document.createElement('div');
                box.style.cssText = `
                    position: fixed !important;
                    z-index: 2147483646 !important;
                    pointer-events: none !important;
                    border: 2px dashed rgba(34, 211, 238, 0.8) !important;
                    background-color: rgba(34, 211, 238, 0.05) !important;
                    top: ${sRect.top}px !important;
                    left: ${sRect.left}px !important;
                    width: ${sRect.width}px !important;
                    height: ${sRect.height}px !important;
                    display: block !important;
                    border-radius: 2px !important;
                `;
                document.documentElement.appendChild(box);
                this.siblingHighlights.push({ target: sibling, box });
            });
        }

        // Update tooltip content
        const tag = element.tagName.toLowerCase();
        const id = element.id ? `#${element.id}` : '';
        const className = element.className && typeof element.className === 'string'
            ? `.${element.className.split(' ')[0]}`
            : '';

        const mainText = label || `${tag}${id}${className}`;
        const secondaryText = subLabel || 'Click to select';

        this.tooltip.innerHTML = `
            <div style="font-weight: bold; margin-bottom: 2px;">${mainText}</div>
            <div style="font-size: 11px; opacity: 0.9;">${secondaryText}</div>
        `;
        this.tooltip.style.display = 'block';

        // Position tooltip based on Anchor (cursor/hovered element) or Main Element
        this.positionTooltip(anchorRect);
    }

    private positionTooltip(rect: DOMRect) {
        if (!this.tooltip) return;
        const tooltipRect = this.tooltip.getBoundingClientRect();
        const padding = 10;
        const viewportWidth = window.innerWidth;
        const viewportHeight = window.innerHeight;

        // Default position: Top Center of rect
        let top = rect.top - tooltipRect.height - padding;
        let left = rect.left + (rect.width / 2) - (tooltipRect.width / 2);

        // If off-screen top, move to bottom
        if (top < padding) {
            top = rect.bottom + padding;
        }

        // Ensure bottom doesn't overflow
        if (top + tooltipRect.height > viewportHeight - padding) {
            // If both top and bottom are tight, stick to bottom of viewport or just clamp
            top = Math.min(top, viewportHeight - tooltipRect.height - padding);
        }

        // Ensure top is never negative (fallback)
        top = Math.max(padding, top);

        // Horizontal Clamping
        left = Math.max(padding, left); // Left edge
        left = Math.min(left, viewportWidth - tooltipRect.width - padding); // Right edge

        this.tooltip.style.top = `${top}px`;
        this.tooltip.style.left = `${left}px`;
    }

    private handleClick(e: MouseEvent) {
        // IGNORE clicks on the interactions editor
        // Since we use capture=true, we interpret the event before it reaches the shadow DOM.
        // We must check if the target is our editor host.

        // Also check composed path for shadow roots
        const path = e.composedPath();
        for (const el of path) {
            if (el instanceof HTMLElement && el.id === 'crawl-pilot-editor-host') {
                return;
            }
        }

        if (!this.isActive || !this.strategy) return;
        e.preventDefault();
        e.stopPropagation();
        e.stopImmediatePropagation();

        // Use the currentElement identified by handleMouseMove
        // because the overlay captures the actual click event target.
        if (this.currentElement) {
            this.strategy.handleClick(this.currentElement, this, (msg) => emit(msg));
        }
    }

    private handleKeyDown(e: KeyboardEvent) {
        // High-priority escape check
        if (e.key === 'Escape') {
            console.log('🛑 [Picker] Escape key detected, cancelling...');
            e.preventDefault();
            e.stopPropagation();
            e.stopImmediatePropagation();
            if (this.onCancelCallback) this.onCancelCallback();
            this.deactivate();
            return;
        }

        if (e.key === 'ArrowUp' && this.isActive && this.currentElement) {
            e.preventDefault();
            e.stopPropagation();
            e.stopImmediatePropagation();
            const parent = this.currentElement.parentElement;
            if (parent && parent !== document.body && parent !== document.documentElement) {
                this.currentElement = parent;
                this.pinned = true;
                if (this.strategy) this.strategy.handleHover(this.currentElement, this);
            }
        }

        if (e.key === 'ArrowDown' && this.isActive && this.currentElement) {
            e.preventDefault();
            e.stopPropagation();
            e.stopImmediatePropagation();
            const children = Array.from(this.currentElement.children) as HTMLElement[];
            const firstMeaningful = children.find(c =>
                hasRenderedBox(c) && (c.innerText || c.textContent || '').trim().length > 0
            );
            if (firstMeaningful) {
                this.currentElement = firstMeaningful;
                this.pinned = true;
                if (this.strategy) this.strategy.handleHover(this.currentElement, this);
            }
        }

        if (e.key === 'Enter' && this.isActive && this.currentElement && this.strategy) {
            e.preventDefault();
            e.stopPropagation();
            e.stopImmediatePropagation();
            this.strategy.handleClick(this.currentElement, this, (msg) => emit(msg));
        }
    }

    private handleScroll() {
        // Track the highlighted container (what the box actually outlines),
        // not the hovered leaf — otherwise the box jumps to the leaf on scroll.
        const tracked = this.lastHighlightedElement || this.currentElement;
        if (tracked && this.highlightBox) {
            const rect = getVisualRect(tracked);
            this.highlightBox.style.top = `${rect.top}px`;
            this.highlightBox.style.left = `${rect.left}px`;
            this.highlightBox.style.width = `${rect.width}px`;
            this.highlightBox.style.height = `${rect.height}px`;
        }
        // Keep sibling boxes glued to their elements while scrolling
        this.siblingHighlights.forEach(({ target, box }) => {
            const r = getVisualRect(target);
            box.style.top = `${r.top}px`;
            box.style.left = `${r.left}px`;
            box.style.width = `${r.width}px`;
            box.style.height = `${r.height}px`;
        });
    }
}
