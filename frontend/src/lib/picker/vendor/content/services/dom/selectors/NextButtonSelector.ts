

export interface ValidationOptions {
    requireVisible?: boolean;
    requireEnabled?: boolean;
    requireInViewport?: boolean;
    expectedByteSize?: number | null;
}

export interface SelectorResult {
    success: boolean;
    index?: number;
    selector?: string;
    selectorType?: string;
    confidence?: string;
    elementInfo?: any;
    element?: HTMLElement;
    error?: string;
    failureReasons?: string[];
}

/**
 * Ported logic from background-readable.js (findClickableElement)
 * Encapsulates the logic for finding and validating the "Next" button using a list of selectors.
 */
export class NextButtonSelector {

    /**
     * Finds the best clickable element that matches one of the provided selectors.
     * Corresponds to `findClickableElement` in background-readable.js
     */
    public static find(
        root: Document | HTMLElement,
        selectors: Array<{ value: string, type: 'selector' | 'xpath' }>,
        elementName: string = 'clickable element',
        options: ValidationOptions = {}
    ): SelectorResult {
        const config = {
            requireVisible: true,
            requireEnabled: true,
            requireInViewport: false,
            expectedByteSize: null,
            ...options
        };

        const failureReasons: string[] = [];

        // Helper to check element size (byte size)
        const getByteSize = (el: HTMLElement) => {
            let size = 0;
            try {
                const s = new XMLSerializer().serializeToString(el);
                size = new Blob([s]).size;
            } catch (e) { }
            return size;
        };

        // Helper to select element
        const selectElement = (sel: string, type: string): HTMLElement | null => {
            if (type === 'selector' || type === 'css') {
                const els = root.querySelectorAll(sel);
                return els.length >= 1 ? els[0] as HTMLElement : null;
            }
            if (type === 'xpath') {
                // Fix: use root as context, and allow multiple (pick first)
                const res = document.evaluate(sel, root, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null);
                return res.singleNodeValue as HTMLElement;
            }
            return null;
        };

        // Helper to count matches (for validation)
        const countMatches = (sel: string, type: string): number => {
            if (type === 'selector' || type === 'css') {
                return root.querySelectorAll(sel).length;
            }
            if (type === 'xpath') {
                try {
                    return document.evaluate(sel, root, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null).snapshotLength;
                } catch (e) { return 0; }
            }
            return 0;
        };

        // Helper validation function
        const validateElement = (el: HTMLElement, opts: ValidationOptions): { valid: boolean; reason?: string; confidence?: string; elementInfo?: any } => {
            if (!el) return { valid: false, reason: 'Element not found' };

            const rect = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);

            if (opts.requireVisible) {
                if (rect.width === 0 || rect.height === 0) return { valid: false, reason: 'Element has zero dimensions' };
                if (style.display === 'none' || style.visibility === 'hidden') return { valid: false, reason: 'Element is hidden' };
                if (style.opacity === '0') return { valid: false, reason: 'Element is transparent' };
            }

            if (opts.requireEnabled) {
                const disabledAttr = el.getAttribute('disabled');
                const isDisabledAttr = disabledAttr !== null && disabledAttr.toString().toLowerCase() !== 'false' && disabledAttr !== '0';

                // Broad disabled class list: generic + Amazon (a-disabled only, NOT a-last which is used on every page) + others
                const DISABLED_CLASSES = ['disabled', 'is-disabled', 'inactive', 'a-disabled', 'unavailable', 'not-active'];
                const hasDisabledClass = DISABLED_CLASSES.some(cls => el.classList.contains(cls));
                const isAriaDisabled = el.getAttribute('aria-disabled') === 'true';
                const hasNotAllowedCursor = style.cursor === 'not-allowed';
                const hasPointerEventsNone = style.pointerEvents === 'none';

                // Also check parent element (e.g. <span class="a-disabled"><a>Next</a></span>)
                const parent = el.parentElement;
                const parentDisabled = parent ? (
                    DISABLED_CLASSES.some(cls => parent.classList.contains(cls)) ||
                    parent.getAttribute('aria-disabled') === 'true'
                ) : false;

                if ((el as any).disabled || isDisabledAttr || hasDisabledClass || isAriaDisabled ||
                    hasNotAllowedCursor || hasPointerEventsNone || parentDisabled) {
                    return { valid: false, reason: 'Element is disabled (attr/prop/class/style/parent)' };
                }
            }

            if (opts.expectedByteSize) {
                const size = getByteSize(el);
                if (size !== opts.expectedByteSize) {
                    return { valid: false, reason: `Element bytesize mismatch: expected ${opts.expectedByteSize} bytes, got ${size} bytes` };
                }
            }

            const tagNameLower = el.tagName.toLowerCase();
            const isLikeButton = tagNameLower === 'button' || tagNameLower === 'a' || el.getAttribute('role') === 'button';
            const hasClickEvt = el.onclick !== null || el.getAttribute('onclick') || el.classList.contains('btn') || el.classList.contains('button');

            // [NEW] Text-based confidence
            const text = el.textContent?.trim().toLowerCase() || '';
            const hasNextText = text.includes('next') || text.includes('>') || text.includes('»');

            let confidence: 'low' | 'medium' | 'high' = 'low';
            if (isLikeButton && hasNextText) confidence = 'high';
            else if (isLikeButton || (hasClickEvt && hasNextText)) confidence = 'medium';

            return {
                valid: true,
                confidence,
                elementInfo: {
                    tagName: el.tagName,
                    id: el.id,
                    className: el.className,
                    text: text.substring(0, 50),
                    position: {
                        x: Math.round(rect.left),
                        y: Math.round(rect.top),
                        width: Math.round(rect.width),
                        height: Math.round(rect.height)
                    },
                    attributes: {
                        disabled: (el as any).disabled,
                        onclick: !!el.onclick,
                        role: el.getAttribute('role'),
                        ariaLabel: el.getAttribute('aria-label'),
                        confidenceScore: this.calculateConfidenceScore(el)
                    },
                    byteSize: opts.expectedByteSize ? getByteSize(el) : undefined
                }
            };
        };

        // Main Loop
        for (let i = 0; i < selectors.length; i++) {
            const { value: sel, type } = selectors[i];
            if (!sel || !type) {
                failureReasons.push(`Selector ${i}: Invalid selector config (missing type or value)`);
                continue;
            }

            try {
                const matchCount = countMatches(sel, type);
                if (matchCount === 0) {
                    // failureReasons.push(`Selector ${i} (${type}): No elements found`);
                    // console.log(`[NextButtonSelector] Failed ${sel}: No matches`);
                    continue;
                }
                // [NEW] Confidence-based selection for multiple matches
                let bestEl: HTMLElement | null = null;
                if (matchCount > 1) {
                    const els = root instanceof Document ?
                        (type === 'xpath' ? this.getAllElementsByXPath(sel, root) : Array.from(root.querySelectorAll(sel))) :
                        (type === 'xpath' ? this.getAllElementsByXPath(sel, root) : Array.from(root.querySelectorAll(sel)));

                    const validEls = (els as HTMLElement[])
                        .map(e => ({ el: e, validation: validateElement(e, config), score: this.calculateConfidenceScore(e) }))
                        .filter(res => res.validation.valid)
                        .sort((a, b) => b.score - a.score);

                    if (validEls.length > 0) {
                        bestEl = validEls[0].el;
                        console.log(`[NextButtonSelector] Multiple valid matches for ${sel}. Picked one with score ${validEls[0].score}`);
                    }
                } else {
                    bestEl = selectElement(sel, type);
                }

                if (!bestEl) {
                    continue;
                }

                const finalValidation = validateElement(bestEl, config);
                if (finalValidation.valid) {
                    console.log(`[NextButtonSelector] Success ${sel}`);
                    return {
                        success: true,
                        index: i,
                        selector: sel,
                        selectorType: type,
                        confidence: finalValidation.confidence,
                        elementInfo: finalValidation.elementInfo,
                        element: bestEl
                    };
                } else {
                    console.log(`[NextButtonSelector] Failed ${sel}: Validation error - ${finalValidation.reason}`);
                    failureReasons.push(`Selector ${i} (${type}): Failed validation - ${finalValidation.reason}`);
                }

            } catch (err: any) {
                console.log(`[NextButtonSelector] Failed ${sel}: Error ${err.message}`);
                failureReasons.push(`Selector ${i} (${type}): Error - ${err.message}`);
            }
        }

        return {
            success: false,
            error: `No valid ${elementName} found after testing ${selectors.length} selectors`,
            failureReasons
        };
    }

    private static calculateConfidenceScore(el: HTMLElement): number {
        let score = 0;
        const tagName = el.tagName.toLowerCase();

        // Base semantics
        if (tagName === 'button') score += 5;
        if (tagName === 'a') score += 4;
        if (el.getAttribute('role') === 'button') score += 5;

        // Classes & Attributes
        if (el.classList.contains('btn') || el.classList.contains('button')) score += 3;
        if (el.onclick !== null || el.getAttribute('onclick')) score += 3;
        if (el.className.toLowerCase().includes('next')) score += 2;
        if (el.getAttribute('aria-label')?.toLowerCase().includes('next')) score += 2;

        // Text content
        const text = el.textContent?.trim().toLowerCase() || '';
        if (text === 'next' || text === 'next >' || text === '>') score += 4;
        else if (text.includes('next')) score += 2;

        return score;
    }

    private static getAllElementsByXPath(xpath: string, root: Document | HTMLElement): HTMLElement[] {
        const results: HTMLElement[] = [];
        const snapshot = document.evaluate(xpath, root, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
        for (let i = 0; i < snapshot.snapshotLength; i++) {
            results.push(snapshot.snapshotItem(i) as HTMLElement);
        }
        return results;
    }
}
