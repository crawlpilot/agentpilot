import type { IContainerDetector, ContainerResult } from './IContainerDetector';
import { hasRenderedBox } from '../../utils/rendering';

/**
 * Simplified container detection using tag homogeneity.
 * Finds the ancestor whose direct children are mostly the same tag (repeating item cards).
 * No getComputedStyle, no getBoundingClientRect, no fingerprinting — just tag counting.
 */
// Form/interactive elements are UI controls, not repeating data cards
const INTERACTIVE_TAGS = new Set(['BUTTON', 'INPUT', 'SELECT', 'OPTION', 'TEXTAREA', 'LABEL']);

export class GroupParentContainerDetector implements IContainerDetector {
    // Deep enough to reach the list container from a leaf inside a card —
    // modern SPAs (LinkedIn) nest card content 12-14 wrapper levels deep, and 8
    // forced users to hover the exact card boundary before detection kicked in.
    private readonly maxLevelsUp = 16;
    private readonly minItems = 2;

    public findContainer(element: HTMLElement): ContainerResult {
        const candidates: Array<{ container: HTMLElement; children: HTMLElement[]; score: number }> = [];
        let current: HTMLElement | null = element;
        let levels = 0;

        while (current && current !== document.body && current !== document.documentElement && levels < this.maxLevelsUp) {
            const children = this.getHomogeneousChildren(current);
            if (children) {
                candidates.push({ container: current, children, score: this.scoreContainer(current, children) });
            }
            current = current.parentElement;
            levels++;
        }

        if (candidates.length === 0) {
            return { isContainer: false, container: null, childCount: 0, siblings: [], confidence: 0 };
        }

        // Best-scored is the default selection
        candidates.sort((a, b) => b.score - a.score);
        const best = candidates[0];

        return {
            isContainer: true,
            containerType: this.containerType(best.container, best.children),
            container: best.container,
            childCount: best.children.length,
            siblings: best.children,
            confidence: 90
        };
    }

    /**
     * Returns visible children sharing a dominant tag (≥60% homogeneity),
     * or null if this element is not a useful container candidate.
     */
    private getHomogeneousChildren(el: HTMLElement): HTMLElement[] | null {
        const visible: HTMLElement[] = [];
        for (const child of el.children) {
            const c = child as HTMLElement;
            // HRs are decorative separators between cards, not items — they'd
            // break the homogeneity ratio on lists that interleave them.
            if (c.tagName === 'HR') continue;
            if (hasRenderedBox(c) && !INTERACTIVE_TAGS.has(c.tagName)) {
                visible.push(c);
            }
        }

        if (visible.length < this.minItems) return null;

        const tagCounts: Record<string, number> = {};
        visible.forEach(c => {
            tagCounts[c.tagName] = (tagCounts[c.tagName] || 0) + 1;
        });

        const dominant = Object.keys(tagCounts).reduce((a, b) => tagCounts[a] >= tagCounts[b] ? a : b);
        const dominantCount = tagCounts[dominant];

        if (dominantCount < this.minItems || dominantCount / visible.length < 0.6) return null;

        return visible.filter(c => c.tagName === dominant);
    }

    private scoreContainer(el: HTMLElement, children: HTMLElement[]): number {
        let score = children.length * 2;

        if (['UL', 'OL', 'DL', 'TABLE', 'TBODY'].includes(el.tagName)) score += 20;
        if (el.getAttribute('role') === 'list') score += 15;

        const cls = (el.className || '').toLowerCase();
        if (/list|grid|card|product|result|item|feed|catalog|collection/.test(cls)) score += 15;

        return score;
    }

    /**
     * Called by DetailSelectionStrategy when user clicks on a container.
     * Checks direct children first, then semantic descendants.
     */
    public findInternalContainer(element: HTMLElement): ContainerResult {
        const NONE: ContainerResult = { isContainer: false, container: null, childCount: 0, siblings: [], confidence: 0 };

        const filterVisible = (els: Iterable<Element>) =>
            Array.from(els).filter(c => {
                const el = c as HTMLElement;
                return hasRenderedBox(el) &&
                    (el.textContent || '').trim().length > 0 &&
                    !INTERACTIVE_TAGS.has(el.tagName);
            }) as HTMLElement[];

        // Gallery wrappers (each item wraps only one child, or only interactive children)
        // should defer to classifyAsHomogeneousArray rather than be labelled list/table.
        const isSimpleWrapper = (els: HTMLElement[]): boolean => {
            if (!els.length) return false;
            const avgDirect = els.reduce((s, e) => s + e.children.length, 0) / els.length;
            if (avgDirect <= 1) return true;
            const avgNonInteractive = els.reduce((s, e) =>
                s + Array.from(e.children).filter(c => !INTERACTIVE_TAGS.has((c as HTMLElement).tagName)).length
            , 0) / els.length;
            return avgNonInteractive === 0;
        };

        // For UL/OL: only treat as a container if items are complex (multi-field data rows).
        // Simple arrays (each li wraps a single image/link/text) defer to classifyAsHomogeneousArray
        // which correctly returns image_array / link_array / text_array.
        if (['UL', 'OL'].includes(element.tagName)) {
            const visible = filterVisible(element.children);
            if (visible.length < this.minItems) return NONE;
            const avgChildCount = visible.reduce((sum, li) => sum + li.children.length, 0) / visible.length;
            if (avgChildCount <= 1.5) return NONE; // simple wrapper → defer to array classifier
            return {
                isContainer: true,
                containerType: this.containerType(element, visible),
                container: element,
                siblings: visible,
                childCount: visible.length,
                confidence: 95
            };
        }

        const visible = filterVisible(element.children);
        if (visible.length >= this.minItems && !isSimpleWrapper(visible)) {
            return {
                isContainer: true,
                containerType: this.containerType(element, visible),
                container: element,
                siblings: visible,
                childCount: visible.length,
                confidence: 95
            };
        }

        for (const list of Array.from(element.querySelectorAll('ul, ol, table, tbody, [role="list"]')) as HTMLElement[]) {
            const valid = filterVisible(list.children);
            if (valid.length >= this.minItems) {
                // Apply same simple-vs-complex check for UL/OL found via descendants
                if (['UL', 'OL'].includes(list.tagName)) {
                    const avgChildCount = valid.reduce((sum, li) => sum + li.children.length, 0) / valid.length;
                    if (avgChildCount <= 1.5) continue; // defer to array classifier
                }
                if (isSimpleWrapper(valid)) continue;
                return {
                    isContainer: true,
                    containerType: this.containerType(list, valid),
                    container: list,
                    siblings: valid,
                    childCount: valid.length,
                    confidence: 90
                };
            }
        }

        // Deep scan (click-only path — performance is acceptable here).
        // Finds the deepest descendant with the most visible non-interactive children,
        // so clicking a wrapper like <div.a-box-group> still surfaces the inner content grid.
        const isVisible = (c: Element) => {
            const el = c as HTMLElement;
            return hasRenderedBox(el) &&
                (el.textContent || '').trim().length > 0 &&
                !INTERACTIVE_TAGS.has(el.tagName);
        };
        const all = element.querySelectorAll('*');
        let bestEl: HTMLElement | null = null;
        let bestKids: HTMLElement[] = [];
        const limit = Math.min(all.length, 300);
        for (let i = 0; i < limit; i++) {
            const desc = all[i] as HTMLElement;
            const kids = Array.from(desc.children).filter(isVisible) as HTMLElement[];
            if (kids.length <= bestKids.length) continue;
            // Skip simple UL/OL wrappers — classifyAsHomogeneousArray handles those
            if (['UL', 'OL'].includes(desc.tagName) && kids.length > 0) {
                const avg = kids.reduce((s, k) => s + k.children.length, 0) / kids.length;
                if (avg <= 1.5) continue;
            }
            if (isSimpleWrapper(kids)) continue;
            bestEl = desc; bestKids = kids;
        }
        if (bestEl && bestKids.length >= this.minItems) {
            return {
                isContainer: true,
                containerType: this.containerType(bestEl, bestKids),
                container: bestEl,
                siblings: bestKids,
                childCount: bestKids.length,
                confidence: 80
            };
        }

        return { isContainer: false, container: null, childCount: 0, siblings: [], confidence: 0 };
    }

    private containerType(container: HTMLElement, siblings: HTMLElement[]): 'list' | 'table' {
        if (['TABLE', 'TBODY', 'THEAD', 'UL', 'OL'].includes(container.tagName)) return 'table';

        if (siblings.length >= 3) {
            const counts = siblings.map(s => s.children.length);
            const multiCell = counts.filter(n => n >= 2 && n <= 10).length;

            if (multiCell / siblings.length >= 0.8) {
                const freq: Record<number, number> = {};
                counts.forEach(n => {
                    freq[n] = (freq[n] || 0) + 1;
                });

                const mode = Number(Object.keys(freq).reduce((a, b) => freq[+a] >= freq[+b] ? a : b));
                if (mode >= 2 && freq[mode] / counts.length >= 0.7) return 'table';
            }
        }

        return 'list';
    }
}
