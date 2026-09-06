/**
 * SelectionHighlightManager
 *
 * Manages persistent visual highlights on the page for elements that the user
 * has already selected in the Page Extractor schema step. This gives users
 * a clear visual reference of what has been selected so far.
 */

import { sanitizeCssSelector } from '../dom/domUtils';

interface HighlightEntry {
    elementDef: HighlightableElement;
    overlayEl: HTMLElement;
    resolvedEl: HTMLElement | null;
}

interface HighlightableElement {
    id: string;
    name: string;
    action: 'extract' | 'click';
    selectors: { type: string; value: string }[];
}

export class SelectionHighlightManager {
    private highlights: Map<string, HighlightEntry> = new Map();
    private scrollHandler: (() => void) | null = null;
    private resizeHandler: (() => void) | null = null;
    private rafId: number | null = null;
    private styleEl: HTMLStyleElement | null = null;

    constructor() {
        this.updatePositions = this.updatePositions.bind(this);
    }

    /**
     * Show highlights for all given elements (replaces any existing highlights)
     */
    public showHighlights(elements: HighlightableElement[]): void {
        this.clearHighlights();
        this.injectStyles();

        elements.forEach((elDef, index) => {
            this.createHighlight(elDef, index);
        });

        this.attachListeners();
    }

    /**
     * Add a single highlight for a newly selected element
     */
    public addHighlight(element: HighlightableElement): void {
        if (this.highlights.has(element.id)) return;

        if (!this.styleEl) this.injectStyles();

        const index = this.highlights.size;
        this.createHighlight(element, index);

        if (!this.scrollHandler) this.attachListeners();
    }

    /**
     * Remove all highlights and clean up listeners
     */
    public clearHighlights(): void {
        this.highlights.forEach(entry => {
            entry.overlayEl.remove();
        });
        this.highlights.clear();
        this.detachListeners();

        if (this.styleEl) {
            this.styleEl.remove();
            this.styleEl = null;
        }
    }

    /**
     * Refresh highlights after element deletion (re-sync with new list)
     */
    public refreshHighlights(elements: HighlightableElement[]): void {
        this.showHighlights(elements);
    }

    // ─── Internal ──────────────────────────────────────────────

    private createHighlight(elDef: HighlightableElement, index: number): void {
        const resolvedEl = this.resolveElement(elDef.selectors);
        if (!resolvedEl) {
            console.warn(`[SelectionHighlightManager] Could not resolve element: ${elDef.name}`);
            return;
        }

        const overlay = document.createElement('div');
        overlay.className = 'crawlpilot-selection-highlight';
        overlay.dataset.highlightId = elDef.id;

        const isClick = elDef.action === 'click';
        const borderColor = isClick ? '#f59e0b' : '#10B981';
        const bgColor = isClick ? 'rgba(245, 158, 11, 0.08)' : 'rgba(16, 185, 129, 0.08)';
        const shadowColor = isClick ? 'rgba(245, 158, 11, 0.3)' : 'rgba(16, 185, 129, 0.3)';

        const rect = resolvedEl.getBoundingClientRect();

        overlay.style.cssText = `
            position: fixed !important;
            top: ${rect.top}px !important;
            left: ${rect.left}px !important;
            width: ${rect.width}px !important;
            height: ${rect.height}px !important;
            border: 2px dashed ${borderColor} !important;
            background-color: ${bgColor} !important;
            z-index: 2147483640 !important;
            pointer-events: none !important;
            border-radius: 4px !important;
            box-shadow: 0 0 8px ${shadowColor} !important;
            transition: top 0.15s ease-out, left 0.15s ease-out, width 0.15s ease-out, height 0.15s ease-out !important;
        `;

        // Name label badge
        const label = document.createElement('div');
        const icon = isClick ? '⚡' : '📌';
        label.textContent = `${icon} ${elDef.name || `Field ${index + 1}`}`;
        label.style.cssText = `
            position: absolute !important;
            top: -24px !important;
            left: 0 !important;
            background: ${borderColor} !important;
            color: ${isClick ? '#000' : '#fff'} !important;
            font-size: 10px !important;
            font-weight: 700 !important;
            padding: 2px 8px !important;
            border-radius: 3px !important;
            font-family: system-ui, -apple-system, sans-serif !important;
            white-space: nowrap !important;
            box-shadow: 0 2px 6px rgba(0,0,0,0.25) !important;
            letter-spacing: 0.3px !important;
        `;
        overlay.appendChild(label);

        // Index badge (bottom-right)
        const indexBadge = document.createElement('div');
        indexBadge.textContent = `${index + 1}`;
        indexBadge.style.cssText = `
            position: absolute !important;
            bottom: -10px !important;
            right: -10px !important;
            width: 20px !important;
            height: 20px !important;
            background: ${borderColor} !important;
            color: ${isClick ? '#000' : '#fff'} !important;
            font-size: 10px !important;
            font-weight: 800 !important;
            border-radius: 50% !important;
            display: flex !important;
            align-items: center !important;
            justify-content: center !important;
            font-family: system-ui, -apple-system, sans-serif !important;
            box-shadow: 0 2px 6px rgba(0,0,0,0.3) !important;
        `;
        overlay.appendChild(indexBadge);

        document.documentElement.appendChild(overlay);

        this.highlights.set(elDef.id, {
            elementDef: elDef,
            overlayEl: overlay,
            resolvedEl: resolvedEl
        });
    }

    private resolveElement(selectors: { type: string; value: string }[]): HTMLElement | null {
        for (const selector of selectors) {
            try {
                if (selector.type === 'xpath') {
                    const result = document.evaluate(
                        selector.value, document, null,
                        XPathResult.FIRST_ORDERED_NODE_TYPE, null
                    );
                    const el = result.singleNodeValue as HTMLElement;
                    if (el) return el;
                } else {
                    const el = document.querySelector(sanitizeCssSelector(selector.value)) as HTMLElement;
                    if (el) return el;
                }
            } catch (e) {
                console.warn(`[SelectionHighlightManager] Failed to resolve selector: ${selector.value}`, e);
            }
        }
        return null;
    }

    private updatePositions(): void {
        this.highlights.forEach(entry => {
            if (!entry.resolvedEl) return;

            const rect = entry.resolvedEl.getBoundingClientRect();
            // Hide if element is no longer visible (removed from DOM or zero-size)
            if (rect.width === 0 && rect.height === 0) {
                entry.overlayEl.style.display = 'none';
                return;
            }

            entry.overlayEl.style.display = 'block';
            entry.overlayEl.style.top = `${rect.top}px`;
            entry.overlayEl.style.left = `${rect.left}px`;
            entry.overlayEl.style.width = `${rect.width}px`;
            entry.overlayEl.style.height = `${rect.height}px`;
        });
    }

    private attachListeners(): void {
        this.scrollHandler = () => {
            if (this.rafId) cancelAnimationFrame(this.rafId);
            this.rafId = requestAnimationFrame(this.updatePositions);
        };
        this.resizeHandler = this.scrollHandler;

        document.addEventListener('scroll', this.scrollHandler, { passive: true, capture: true });
        window.addEventListener('resize', this.resizeHandler, { passive: true });
    }

    private detachListeners(): void {
        if (this.scrollHandler) {
            document.removeEventListener('scroll', this.scrollHandler, true);
        }
        if (this.resizeHandler) {
            window.removeEventListener('resize', this.resizeHandler);
        }
        if (this.rafId) {
            cancelAnimationFrame(this.rafId);
            this.rafId = null;
        }
        this.scrollHandler = null;
        this.resizeHandler = null;
    }

    private injectStyles(): void {
        if (document.getElementById('crawlpilot-selection-highlight-styles')) return;

        this.styleEl = document.createElement('style');
        this.styleEl.id = 'crawlpilot-selection-highlight-styles';
        this.styleEl.textContent = `
            .crawlpilot-selection-highlight {
                animation: crawlpilot-sel-fadein 0.3s ease-out;
            }
            @keyframes crawlpilot-sel-fadein {
                from { opacity: 0; transform: scale(0.95); }
                to { opacity: 1; transform: scale(1); }
            }
        `;
        document.head.appendChild(this.styleEl);
    }
}
