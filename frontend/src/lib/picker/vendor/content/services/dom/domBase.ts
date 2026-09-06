
// Stability heuristics (isUnstableClass, isHashClass, isStableAttributeValue) moved to
// src/shared/selectors/stability.ts — the canonical, testable home shared by content and
// background code. Re-exported here so existing imports of `domBase` keep working.
import { isUnstableClass, isHashClass, isStableAttributeValue, isVolatileSelector } from '../../../shared/selectors/stability';
export { isUnstableClass, isHashClass, isStableAttributeValue, isVolatileSelector };

export function generateCssSelector(el: HTMLElement, generalized: boolean = false): string {
    if (!el || !el.tagName) return '';

    // 1. ID (Strongest) - Only use if NOT generalized
    // [CHANGED] Stricter ID checks
    if (!generalized && el.id && !/\d/.test(el.id) && !/(Instance|ember|react|guid|uid|_)/i.test(el.id) && el.id.length < 40) {
        try {
            const escapedId = CSS.escape(el.id);
            if (document.querySelectorAll(`#${escapedId}`).length === 1) {
                return `#${escapedId}`;
            }
        } catch (e) {
            // Ignore invalid IDs if CSS.escape fails or selector is still bad
        }
    }

    // 2. Path-based generation
    const path: string[] = [];
    let current: HTMLElement | null = el;
    let depth = 0;

    while (current && current !== document.body && depth < 10) {
        let selector = current.tagName.toLowerCase();

        // Add classes if available
        if (current.className && typeof current.className === 'string') {
            const classes = current.className.split(/\s+/).filter(c => c && !isUnstableClass(c));

            if (classes.length > 0) {
                selector += `.${classes[0]}`;
            }
        }

        if (current.parentElement && !generalized) {
            const siblings = Array.from(current.parentElement.children);
            const sameTagSiblings = siblings.filter(s => s.tagName === current!.tagName);

            if (sameTagSiblings.length > 1) {
                const index = sameTagSiblings.indexOf(current);
                selector += `:nth-of-type(${index + 1})`;
            }
        }

        path.unshift(selector);
        current = current.parentElement;
        depth++;
    }

    return path.join(' > ');
}

export function generateXPath(element: HTMLElement): string {
    if (!element || element.nodeType !== 1) return '';

    // 1. If element has ID, use it for shortest path (IF STABLE)
    // [CHANGED] Stricter ID checks
    if (element.id && !/\d/.test(element.id) && !/(Instance|ember|react|guid|uid|_)/i.test(element.id) && element.id.length < 40) {
        // Simple quote escaping - though usually IDs don't contain quotes
        return `//*[@id="${element.id}"]`;
    }

    // 2. Iterative path generation
    const paths: string[] = [];
    let current: HTMLElement | null = element;

    while (current && current.nodeType === 1) {
        // If we hit an element with ID during ascent, use it as anchor (IF STABLE)
        if (current.id && !/\d/.test(current.id) && !/(Instance|ember|react|guid|uid|_)/i.test(current.id) && current.id.length < 40) {
            paths.unshift(`*[@id="${current.id}"]`);
            return '//' + paths.join('/');
        }

        if (current === document.body) {
            paths.unshift('html', 'body');
            return '/' + paths.join('/');
        }

        let index = 0;
        const tagName = current.tagName.toLowerCase();

        // Count previous siblings with same tag
        let sibling = current.previousElementSibling;
        while (sibling) {
            if (sibling.tagName.toLowerCase() === tagName) {
                index++;
            }
            sibling = sibling.previousElementSibling;
        }

        paths.unshift(`${tagName}[${index + 1}]`);
        current = current.parentElement;
    }

    return paths.length ? '/' + paths.join('/') : '';
}
