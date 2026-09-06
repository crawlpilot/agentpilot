import { cssSelectorGenerator } from 'css-selector-generator';
import { generateCssSelector, generateXPath, isUnstableClass, isStableAttributeValue } from '../domBase';

export interface SelectorResult {
    selector: string;
    strategy: string;
}

export interface SelectorStrategy {
    name: string;
    generate(element: HTMLElement, options?: SelectorOptions): string[];
    priority: number; // Higher is better
}

export interface SelectorOptions {
    root?: HTMLElement;
    isList?: boolean;
}

export class IdSelectorStrategy implements SelectorStrategy {
    name = 'ID';
    priority = 110;
    generate(element: HTMLElement, options?: SelectorOptions): string[] {
        if (options?.isList) return []; // IDs are usually unique, don't use for list items

        const id = element.id;
        if (!id) return [];

        // [CHANGED] Stricter Stability Checks
        // Reject if:
        // 1. Contains digits (often auto-gen)
        // 2. Contains "Instance", "ember", "react", "guid", "uid" (framework gen)
        // 3. Very long (> 40 chars)
        if (/\d/.test(id)) return [];
        if (/(Instance|ember|react|guid|uid|_)/i.test(id)) return [];
        if (id.length > 40) return [];

        if (document.querySelectorAll(`#${id}`).length === 1) {
            return [`#${id}`];
        }
        return [];
    }
}


/**
 * Tries the element's own tag + semantic class/attribute without any ancestor chain.
 * If the resulting selector is unique within root, it's the shortest stable selector possible.
 */
export class MinimalSelectorStrategy implements SelectorStrategy {
    name = 'Minimal';
    priority = 115;

    generate(element: HTMLElement, options?: SelectorOptions): string[] {
        const root: Element = options?.root || document.body;
        const tag = element.tagName.toLowerCase();
        const results: string[] = [];

        // 1. Semantic attributes directly on the element (stable values only)
        const semanticAttrs = ['data-testid', 'data-cy', 'data-component', 'data-id', 'role', 'aria-label', 'itemprop', 'name'];
        for (const attr of semanticAttrs) {
            const val = element.getAttribute(attr);
            if (val && isStableAttributeValue(attr, val)) {
                const safe = val.replace(/"/g, '\\"');
                const sel = `${tag}[${attr}="${safe}"]`;
                if (this.isUnique(sel, root)) { results.push(sel); break; }
                const noTagSel = `[${attr}="${safe}"]`;
                if (this.isUnique(noTagSel, root)) { results.push(noTagSel); break; }
            }
        }

        // 2. tag + semantic class(es) — no ancestor context
        const classes = element.className && typeof element.className === 'string'
            ? element.className.split(/\s+/).filter(c => c && !isUnstableClass(c))
            : [];

        if (classes.length > 0) {
            const full = `${tag}.${classes.join('.')}`;
            if (this.isUnique(full, root)) results.push(full);
            for (const c of classes) {
                const single = `${tag}.${c}`;
                if (this.isUnique(single, root)) { results.push(single); break; }
            }
        }

        // 3. Ancestor prefix fallback — when tag.class alone isn't unique (e.g. hidden DOM copies)
        if (results.length === 0 && classes.length > 0) {
            const withAncestor = this.tryWithSemanticAncestor(element, tag, classes, root);
            if (withAncestor) results.push(withAncestor);
        }

        return results;
    }

    private tryWithSemanticAncestor(element: HTMLElement, tag: string, classes: string[], root: Element): string | null {
        const targetClasses = classes.slice(0, 3);
        let ancestor = element.parentElement;
        for (let depth = 0; depth < 3 && ancestor && ancestor !== root; depth++) {
            if (ancestor.className && typeof ancestor.className === 'string') {
                const ancestorClasses = ancestor.className.split(/\s+/)
                    .filter(c => c && !isUnstableClass(c))
                    .slice(0, 5);
                for (const ac of ancestorClasses) {
                    for (const c of targetClasses) {
                        const sel = `.${ac} ${tag}.${c}`;
                        try { if (root.querySelectorAll(sel).length === 1) return sel; } catch { }
                    }
                }
            }
            ancestor = ancestor.parentElement;
        }
        return null;
    }

    private isUnique(sel: string, root: Element): boolean {
        try { return root.querySelectorAll(sel).length === 1; } catch { return false; }
    }
}


export class GeneratorLibraryStrategy implements SelectorStrategy {
    name = 'CSS Gen';
    priority = 70;

    generate(element: HTMLElement, options?: SelectorOptions): string[] {
        try {
            const root = options?.root || document.body;
            const genOptions: any = {
                root,
                // Semantic types first — attribute ([data-*]) beats class beats tag
                selectors: options?.isList
                    ? ['attribute', 'class', 'tag']
                    : ['id', 'attribute', 'class', 'tag', 'nthchild'],

                // Boost data-* / aria / role selectors so they rank first
                whitelist: [
                    /^\[data-/,
                    /^\[role/,
                    /^\[aria-/,
                    /^\[itemprop/,
                    /^\[name/,
                ],

                // Block unstable class patterns — function form checks anywhere in selector string
                blacklist: [
                    // Tailwind JIT / v4 arbitrary classes appear CSS-escaped as .\[...\] or .\(...\)
                    (sel: string) => /\\\[/.test(sel),  // e.g. .\[--border-width\:1px\]
                    (sel: string) => /\\\(/.test(sel),  // e.g. .\(--border-width\)
                    (sel: string) => /\\\*/.test(sel),  // e.g. .\*\:-m-
                    (sel: string) => /[_-][a-z]{1,4}\d{2,}/.test(sel),      // versioned slugs (ss26, ab12) anywhere
                    (sel: string) => /\blayout-/.test(sel),                   // layout-* anywhere
                    (sel: string) => /^[a-z]{2,4}-(layout|container|wrapper|main|content|page|section|view)/.test(sel), // design system prefixes
                    (sel: string) => /-(desktop|mobile|tablet|std|alt|compact|wide|narrow)(-|$)/.test(sel), // variant suffixes
                    /^\.[a-z]{1,3}-\d/,                        // w-4, h-8, p-2
                    /^\.[mp][xytrbl]?-/,                       // mx-, py-
                    /^\.(flex|grid|block|inline|hidden|visible|overflow|static|fixed|absolute|relative|sticky)(-|$)/,
                    /^\.(bg-|border-|rounded|ring|shadow|opacity|text-[a-z]+-?\d|gap-|space-)(-|$)/,
                    /^\.(transition|animate|scale|rotate|translate|transform)(-|$)/,
                    /^\.(sc-|css-)/,                           // styled-components / CSS modules
                    /^\.(hover|focus|active|group|peer)(-|$)/, // state/group utilities
                ],

                combineWithinSelector: true,
                combineBetweenSelectors: true,
                // Cap exponential growth on Tailwind-heavy pages
                maxCombinations: 50,
                maxCandidates: 150,
            };

            // cssSelectorGenerator yields candidates simplest → most complex.
            // Collect up to 10, score each, return sorted so generateAll picks the best.
            const candidates: string[] = [];
            for (const sel of cssSelectorGenerator(element, genOptions)) {
                candidates.push(sel);
                if (candidates.length >= 10) break;
            }

            return candidates.sort((a, b) => this.scoreSelector(b) - this.scoreSelector(a));
        } catch {
            return [];
        }
    }

    private scoreSelector(sel: string): number {
        let score = 100;
        // Penalise ancestor depth
        score -= (sel.match(/>/g) || []).length * 15;
        // Penalise positional selectors
        if (sel.includes(':nth-child') || sel.includes(':nth-of-type')) score -= 20;
        // Reward semantic attributes
        if (sel.includes('[data-')) score += 30;
        if (sel.includes('[role') || sel.includes('[aria-')) score += 20;
        if (sel.includes('#')) score += 25;
        // Penalise long selectors
        score -= Math.max(0, sel.length - 40) * 0.5;
        return score;
    }
}

export class SemanticXPathStrategy implements SelectorStrategy {
    name = 'Semantic XPath';
    priority = 106;

    generate(element: HTMLElement, options?: SelectorOptions): string[] {
        if (options?.isList) return []; // CommonSelectorGenerator handles XPath for list items
        const tag = element.tagName.toLowerCase();
        const results: string[] = [];

        // 1. Stable attribute XPath
        const semanticAttrs = ['data-testid', 'data-cy', 'data-component', 'role', 'itemprop', 'name', 'type'];
        for (const attr of semanticAttrs) {
            const val = element.getAttribute(attr);
            if (val && isStableAttributeValue(attr, val)) {
                const safe = val.replace(/"/g, '&quot;');
                const sel = `//${tag}[@${attr}="${safe}"]`;
                if (this.isUnique(sel)) { results.push(sel); return results; }
            }
        }

        // 2. Class-based XPath — contains(concat(...)) prevents partial class name matches
        if (element.className && typeof element.className === 'string') {
            const classes = element.className.split(/\s+/).filter(c => c && !isUnstableClass(c)).slice(0, 5);
            for (const cls of classes) {
                const sel = `//${tag}[contains(concat(' ', normalize-space(@class), ' '), ' ${cls} ')]`;
                if (this.isUnique(sel)) { results.push(sel); return results; }
            }

            // 3. Ancestor prefix fallback when no single class is globally unique
            if (results.length === 0 && classes.length > 0) {
                const targetCls = classes.slice(0, 3);
                let ancestor = element.parentElement;
                for (let depth = 0; depth < 3 && ancestor && ancestor !== document.body; depth++) {
                    if (ancestor.className && typeof ancestor.className === 'string') {
                        const acs = ancestor.className.split(/\s+/)
                            .filter(c => c && !isUnstableClass(c))
                            .slice(0, 5);
                        const atag = ancestor.tagName.toLowerCase();
                        for (const ac of acs) {
                            for (const cls of targetCls) {
                                const sel = `//${atag}[contains(concat(' ', normalize-space(@class), ' '), ' ${ac} ')]//${tag}[contains(concat(' ', normalize-space(@class), ' '), ' ${cls} ')]`;
                                if (this.isUnique(sel)) { results.push(sel); return results; }
                            }
                        }
                    }
                    ancestor = ancestor.parentElement;
                }
            }
        }

        return results;
    }

    private isUnique(sel: string): boolean {
        try {
            const result = document.evaluate(sel, document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
            return result.snapshotLength === 1;
        } catch { return false; }
    }
}

export class XPathStrategy implements SelectorStrategy {
    name = 'XPath';
    priority = 50;
    generate(element: HTMLElement): string[] {
        const xpath = generateXPath(element);
        return xpath ? [xpath] : [];
    }
}

export class AttributeStrategy implements SelectorStrategy {
    name = 'Attribute';
    priority = 108;
    generate(element: HTMLElement): string[] {
        const results: string[] = [];
        // [CHANGED] Explicit whitelist of useful semantic attributes, excluding style/width/height/etc
        const attributes = ['data-testid', 'data-cy', 'data-component-type', 'name', 'role', 'aria-label', 'title', 'rel', 'href', 'type'];

        const tag = element.tagName.toLowerCase();
        for (const attr of attributes) {
            const val = element.getAttribute(attr);
            if (val !== null && isStableAttributeValue(attr, val)) {
                const safeVal = val.replace(/"/g, '\\"');
                results.push(`[${attr}="${safeVal}"]`);
                results.push(`${tag}[${attr}="${safeVal}"]`);
            }
            // Unstable values: skip entirely — [attr] presence selectors match too broadly
        }
        return results;
    }
}

export class PathSelectorStrategy implements SelectorStrategy {
    name = 'Path';
    priority = 65;
    generate(element: HTMLElement): string[] {
        const results: string[] = [];
        const specific = generateCssSelector(element, false);
        const generalized = generateCssSelector(element, true);
        if (specific) results.push(specific);
        if (generalized && generalized !== specific) results.push(generalized);
        return results;
    }
}

export class SelectorGenerator {
    private strategies: SelectorStrategy[] = [
        new MinimalSelectorStrategy(),    // 115 — shortest unique tag+class, no ancestors
        new IdSelectorStrategy(),          // 110 — stable IDs
        new AttributeStrategy(),           // 108 — data-*, aria-*, role
        new SemanticXPathStrategy(),       // 106 — class/attribute XPath
        new GeneratorLibraryStrategy(),   // 70  — css-selector-generator with filter+score
        new PathSelectorStrategy(),        // 65  — path-based CSS fallback
        new XPathStrategy()               // 50  — positional XPath fallback
    ];

    public generateAll(element: HTMLElement, options?: SelectorOptions): SelectorResult[] {
        const results: SelectorResult[] = [];
        const seen = new Set<string>();
        const root: Element = options?.root || document.body;
        let hasCss = false;
        let hasXPath = false;

        const sorted = [...this.strategies].sort((a, b) => b.priority - a.priority);

        for (const strategy of sorted) {
            // Once we have both CSS + XPath from high-quality strategies (priority ≥ 70),
            // skip the remaining fallback strategies (PathSelector 65, XPath 50).
            if (hasCss && hasXPath && strategy.priority < 70) break;

            const candidates = strategy.generate(element, options);
            for (const selector of candidates) {
                if (!selector || seen.has(selector)) continue;
                const trimmed = this.shortenSelector(selector, root);
                if (seen.has(trimmed)) { seen.add(selector); continue; }
                seen.add(trimmed);
                seen.add(selector);
                const isXPath = trimmed.startsWith('/') || trimmed.startsWith('(') || trimmed.startsWith('./');

                // In non-list mode, reject CSS selectors that aren't unique within root.
                // This prevents generic selectors like [aria-label] or [role="img"] from
                // being used when they match multiple elements on the page.
                if (!isXPath && !options?.isList) {
                    try {
                        if (root.querySelectorAll(trimmed).length !== 1) continue;
                    } catch { continue; }
                }

                results.push({ selector: trimmed, strategy: strategy.name });
                if (isXPath) hasXPath = true; else hasCss = true;
            }
        }

        return results;
    }

    /**
     * Strips ancestor segments from the left of a ">" chain until the shortest
     * form that still uniquely matches within root is found.
     */
    private shortenSelector(selector: string, root: Element): string {
        if (!selector.includes('>')) return selector;
        const parts = selector.split(/\s*>\s*/);
        for (let i = 1; i < parts.length; i++) {
            const shorter = parts.slice(i).join(' > ');
            try {
                if (root.querySelectorAll(shorter).length === 1) return shorter;
            } catch { /* invalid intermediate — keep going */ }
        }
        return selector;
    }

    public generateBest(element: HTMLElement, options?: SelectorOptions): string {
        const results = this.generateAll(element, options);
        return results.length > 0 ? results[0].selector : element.tagName.toLowerCase();
    }
}
