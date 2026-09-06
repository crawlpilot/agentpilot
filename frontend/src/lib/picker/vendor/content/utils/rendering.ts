/**
 * True when the element paints content on the page. Unlike a bare
 * offsetWidth/offsetHeight check this also accepts `display: contents`
 * elements: they generate no box of their own (0x0, offsetParent null)
 * but their children render normally. LinkedIn's 2026 web wraps every
 * list card in a `display: contents` div, so filtering on box size alone
 * drops the entire card.
 */
export function hasRenderedBox(el: HTMLElement): boolean {
    if (el.offsetWidth > 0 || el.offsetHeight > 0) return true;
    // Only elements with children can render anything through display:contents;
    // this also avoids the getComputedStyle call for ordinary hidden leaves.
    if (el.childElementCount === 0) return false;
    return getComputedStyle(el).display === 'contents';
}

/**
 * Rect an element visually occupies. For normal elements this is just
 * getBoundingClientRect(); for boxless elements (display: contents) it is
 * the union of the children's rects, so highlight overlays don't collapse
 * to a 0x0 point at the page origin.
 */
export function getVisualRect(el: HTMLElement): DOMRect {
    const rect = el.getBoundingClientRect();
    if (rect.width > 0 || rect.height > 0 || el.childElementCount === 0) return rect;

    let top = Infinity, left = Infinity, bottom = -Infinity, right = -Infinity;
    for (const child of Array.from(el.children) as HTMLElement[]) {
        // Recurse so stacked boxless wrappers still resolve to their content
        const r = getVisualRect(child);
        if (r.width === 0 && r.height === 0) continue;
        top = Math.min(top, r.top);
        left = Math.min(left, r.left);
        bottom = Math.max(bottom, r.bottom);
        right = Math.max(right, r.right);
    }
    if (top === Infinity) return rect;
    return new DOMRect(left, top, right - left, bottom - top);
}
