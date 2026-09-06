import { isUnstableClass, isHashClass, isStableAttributeValue } from '../../../shared/selectors/stability';

/**
 * Builds a stable label for a node used to key extracted fields (part of the path
 * that becomes the column identity). Previously this was just the element's first
 * CSS class, which breaks whenever a site's build hashes class names (CSS-in-JS,
 * CSS modules) or reorders classes — columns would silently rename themselves on
 * every extraction. Priority: stable semantic attribute > semantic tag > first
 * non-volatile class > tag+role > bare tag.
 */
export function generateNodeName(t: HTMLElement): string {
    for (const attr of ['data-testid', 'data-cy', 'itemprop']) {
        const val = t.getAttribute(attr);
        if (val && isStableAttributeValue(attr, val)) return val;
    }

    const tag = t.tagName.toLowerCase();
    if (/^h[1-6]$/.test(tag) || tag === 'a' || tag === 'img' || tag === 'time' || tag === 'button') {
        return tag;
    }

    const classes = (t.getAttribute('class') || '').split(/\s+/).filter(c =>
        c && !isUnstableClass(c) && !isHashClass(c) && !/^\d+$/.test(c) &&
        !['js-', 'data-'].some(exc => c.startsWith(exc))
    );
    if (classes.length > 0) return classes[0];

    const role = t.getAttribute('role');
    if (role) return `${tag}[${role}]`;

    return tag;
}
