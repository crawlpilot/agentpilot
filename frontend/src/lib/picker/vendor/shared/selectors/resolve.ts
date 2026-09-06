import type { ChainEntry, RawChainEntry } from './types';
import { sanitizeCssSelector } from './sanitize';

/** True if a selector string looks like an XPath expression rather than CSS. */
export function looksLikeXPath(value: string): boolean {
    return value.startsWith('/') || value.startsWith('(') || value.startsWith('./') || value.startsWith('.//');
}

/**
 * Normalizes any of the selector-chain shapes used across the codebase
 * (SelectorMetadata {selector,strategy}, SelectorDefinition {value,type}, or a bare string)
 * into a single ChainEntry[] the resolver can walk in order.
 */
export function normalizeChain(entries: (RawChainEntry | null | undefined)[] | null | undefined): ChainEntry[] {
    if (!entries) return [];
    const out: ChainEntry[] = [];
    for (const entry of entries) {
        if (!entry) continue;
        if (typeof entry === 'string') {
            if (!entry) continue;
            out.push({ value: entry, kind: looksLikeXPath(entry) ? 'xpath' : 'css' });
            continue;
        }
        if ('selector' in entry) {
            if (!entry.selector) continue;
            out.push({
                value: entry.selector,
                kind: looksLikeXPath(entry.selector) ? 'xpath' : 'css',
                strategy: entry.strategy,
            });
            continue;
        }
        if ('value' in entry) {
            if (!entry.value) continue;
            const kind = entry.type === 'xpath' ? 'xpath'
                : entry.type === 'css' || entry.type === 'id' ? 'css'
                : looksLikeXPath(entry.value) ? 'xpath' : 'css';
            out.push({ value: entry.value, kind });
        }
    }
    return out;
}

function queryAll(entry: ChainEntry, root: ParentNode): Element[] {
    try {
        if (entry.kind === 'xpath') {
            const doc = root.ownerDocument ?? (root as unknown as Document);
            const result = doc.evaluate(entry.value, root, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
            const out: Element[] = [];
            for (let i = 0; i < result.snapshotLength; i++) {
                const node = result.snapshotItem(i);
                if (node instanceof Element) out.push(node);
            }
            return out;
        }
        return Array.from(root.querySelectorAll(sanitizeCssSelector(entry.value)));
    } catch {
        return [];
    }
}

/**
 * Resolves the first chain entry that matches at least one element, in priority order.
 * This is the single fallback-chain applier for the whole extension — replaces the
 * hand-rolled first-match loops that used to be duplicated in ElementProcessor,
 * BackgroundElementProcessor, and PaginationAction.
 */
export function resolveFirst(
    entries: (RawChainEntry | null | undefined)[] | null | undefined,
    root: ParentNode = document,
): { element: Element | null; used: ChainEntry | null } {
    const chain = normalizeChain(entries);
    for (const entry of chain) {
        const matches = queryAll(entry, root);
        if (matches.length > 0) {
            return { element: matches[0], used: entry };
        }
    }
    return { element: null, used: null };
}

/**
 * Resolves all elements matched by a chain entry, preferring — in order — a chain entry
 * that is not volatile (see isVolatileSelector) and whose match count is closest to
 * `expectedCount` when provided. Falls back to the first entry with any matches.
 */
export function resolveAll(
    entries: (RawChainEntry | null | undefined)[] | null | undefined,
    root: ParentNode = document,
    opts?: { expectedCount?: number },
): { elements: Element[]; used: ChainEntry | null } {
    const chain = normalizeChain(entries);
    const candidates: { entry: ChainEntry; elements: Element[] }[] = [];

    for (const entry of chain) {
        const elements = queryAll(entry, root);
        if (elements.length > 0) candidates.push({ entry, elements });
    }

    if (candidates.length === 0) return { elements: [], used: null };
    if (opts?.expectedCount == null) {
        const best = candidates[0];
        return { elements: best.elements, used: best.entry };
    }

    const expected = opts.expectedCount;
    let best = candidates[0];
    let bestDelta = Math.abs(best.elements.length - expected);
    for (const cand of candidates.slice(1)) {
        const delta = Math.abs(cand.elements.length - expected);
        if (delta < bestDelta) {
            best = cand;
            bestDelta = delta;
        }
    }
    return { elements: best.elements, used: best.entry };
}
