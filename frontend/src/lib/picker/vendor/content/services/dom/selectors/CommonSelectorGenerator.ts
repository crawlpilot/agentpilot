import { generateRobustSelectors } from '../domUtils';
import { isUnstableClass, isStableAttributeValue, isHashClass } from '../domBase';
import { isVolatileSelector } from '../../../../shared/selectors/stability';

export interface DerivedSelector {
    selector: string;
    type: 'CSS' | 'XPath';
}

export class CommonSelectorGenerator {

    /**
     * Derives a list of optimal CSS and XPath selectors that target all the provided elements.
     * Uses generateRobustSelectors internally and filters them based on the exact visual count.
     * 
     * [IMPROVED] Uses similarity scoring to filter out spacers/dividers from the target set
     * before generating common selectors.
     */
    public deriveCommonSelector(elements: HTMLElement[], root: HTMLElement, targetElement?: HTMLElement): DerivedSelector[] {
        if (!elements || elements.length === 0) return [];

        // --- PHASE 0: Filter by Similarity (LinkedIn Spacer Fix) ---
        // If a targetElement is provided (the specific item the user clicked), 
        // we filter the siblings to only include those that are "similar" to it.
        let targetSet = elements;
        if (targetElement) {
            targetSet = elements.filter(el => this.calculateSimilarityScore(el, targetElement) >= 30);

            // If filtering killed everything except the target, fallback to original set 
            // (safety for cases where every item is unique)
            if (targetSet.length < 2) targetSet = elements;
        }

        const targetCount = targetSet.length;
        const validResults: DerivedSelector[] = [];

        // 1. Leverage the robust SelectorGenerator
        const anchor = targetElement || targetSet[0];
        const candidates = generateRobustSelectors(anchor, {
            isList: true,
            root: root
        });

        for (const cand of candidates) {
            if (this.validateSelectorMajority(cand.selector, root, targetCount)) {
                validResults.push({
                    selector: cand.selector,
                    type: this.isXPath(cand.selector) ? 'XPath' : 'CSS'
                });
            }
        }

        // 2. Add manual common semantic fallbacks
        const commonClasses = this.findAllCommonClasses(targetSet);
        for (const cls of commonClasses) {
            if (this.validateSelectorMajority(cls, root, targetCount)) {
                validResults.push({ selector: cls, type: 'CSS' });
            }

            const className = cls.substring(1);
            const xpathSelector = `//${targetSet[0].tagName.toLowerCase()}[contains(concat(' ', normalize-space(@class), ' '), ' ${className} ')]`;
            if (this.validateSelectorMajority(xpathSelector, root, targetCount)) {
                validResults.push({ selector: xpathSelector, type: 'XPath' });
            }
        }

        const commonAttrs = this.findAllCommonAttributes(targetSet);
        for (const attr of commonAttrs) {
            if (this.validateSelectorMajority(attr, root, targetCount)) {
                validResults.push({ selector: attr, type: 'CSS' });
            }

            let xpathAttr = '';
            const tag = targetSet[0].tagName.toLowerCase();
            if (attr.includes('=')) {
                const parts = attr.match(/\[(.*)="(.*)"\]/);
                if (parts) xpathAttr = `//${tag}[@${parts[1]}="${parts[2]}"]`;
            } else {
                const parts = attr.match(/\[(.*)\]/);
                if (parts) xpathAttr = `//${tag}[@${parts[1]}]`;
            }
            if (xpathAttr && this.validateSelectorMajority(xpathAttr, root, targetCount)) {
                validResults.push({ selector: xpathAttr, type: 'XPath' });
            }
        }

        const commonTag = this.findCommonTag(targetSet);
        if (commonTag) {
            if (this.validateSelectorMajority(commonTag, root, targetCount)) {
                validResults.push({ selector: commonTag, type: 'CSS' });
            }
            const xpathTag = `//${commonTag}`;
            if (this.validateSelectorMajority(xpathTag, root, targetCount)) {
                validResults.push({ selector: xpathTag, type: 'XPath' });
            }
        }

        // 2.5 Direct-child candidates — essential on obfuscated/atomic-class DOMs
        // (LinkedIn) where every "common" class also appears on nested elements, so
        // all descendant-scoped candidates overshoot and fail majority validation.
        // `:scope > ...` only matches the items themselves. Downstream resolvers all
        // query relative to the container element, and in chains resolved against
        // `document` a `:scope` entry simply matches nothing and falls through.
        if (commonTag) {
            const directCandidates = [`:scope > ${commonTag}`];
            for (const cls of commonClasses) {
                directCandidates.push(`:scope > ${commonTag}${cls}`);
            }
            for (const attr of commonAttrs) {
                directCandidates.push(`:scope > ${commonTag}${attr}`);
            }
            for (const sel of directCandidates) {
                if (this.validateSelectorMajority(sel, root, targetCount)) {
                    validResults.push({ selector: sel, type: 'CSS' });
                }
            }
        }

        // 3. FR-6: Child-Based Targeting ("CSS :has") — skip for large lists (expensive DOM traversal)
        const childFeatures = targetSet.length <= 15 ? this.findCommonChildFeatures(targetSet) : [];
        for (const feature of childFeatures) {
            const baseTag = targetSet[0].tagName.toLowerCase();
            const hasSelector = `${baseTag}:has(${feature})`;

            if (this.validateSelectorMajority(hasSelector, root, targetCount)) {
                validResults.push({ selector: hasSelector, type: 'CSS' });
            }

            if (commonClasses.length > 0) {
                const baseClass = commonClasses[0];
                const classHasSelector = `${baseClass}:has(${feature})`;
                if (this.validateSelectorMajority(classHasSelector, root, targetCount)) {
                    validResults.push({ selector: classHasSelector, type: 'CSS' });
                }
            }
        }

        // Sort non-volatile candidates (no hash class, no :nth-child) before volatile ones —
        // a saved recipe must never commit to a hash class or positional selector as its
        // primary choice, since those rot across redeploys / DOM reorders. Within each
        // group, sort by quality descending so the best candidate is first.
        validResults.sort((a, b) => {
            const aVolatile = isVolatileSelector(a.selector);
            const bVolatile = isVolatileSelector(b.selector);
            if (aVolatile !== bVolatile) return aVolatile ? 1 : -1;
            return this.scoreSelectorQuality(b.selector) - this.scoreSelectorQuality(a.selector);
        });

        // Deduplicate (after sorting so quality order is preserved)
        const seen = new Set<string>();
        return validResults.filter(item => {
            if (seen.has(item.selector)) return false;
            seen.add(item.selector);
            return true;
        });
    }

    /**
     * Calculates a similarity score (0-60+) between two elements.
     * Based on the robust comparison logic in background-readable.js (Module 588).
     */
    private calculateSimilarityScore(el: HTMLElement, reference: HTMLElement): number {
        let score = 0;

        // 1. Tag match (Higher weight)
        if (el.tagName === reference.tagName) score += 10;

        // 2. ClassList overlap
        const elClasses = new Set(el.className.split(/\s+/).filter(Boolean));
        const refClasses = reference.className.split(/\s+/).filter(Boolean);
        const overlap = refClasses.filter(c => elClasses.has(c)).length;
        if (overlap > 0 && refClasses.length > 0) {
            score += Math.floor((overlap / refClasses.length) * 15);
        }

        // 3. Dimensions (indicates if it's a "card" or a "spacer")
        let elRect = el.getBoundingClientRect();
        let refRect = reference.getBoundingClientRect();

        // If offsetHeight is 0 (display: contents), try to find a child with dimensions
        if (elRect.width === 0 && elRect.height === 0 && el.children.length > 0) {
            const firstWithDim = Array.from(el.querySelectorAll('*')).find(child => child.getBoundingClientRect().width > 0) as HTMLElement;
            if (firstWithDim) elRect = firstWithDim.getBoundingClientRect();
        }
        if (refRect.width === 0 && refRect.height === 0 && reference.children.length > 0) {
            const firstWithDim = Array.from(reference.querySelectorAll('*')).find(child => child.getBoundingClientRect().width > 0) as HTMLElement;
            if (firstWithDim) refRect = firstWithDim.getBoundingClientRect();
        }

        const sameWidth = Math.abs(elRect.width - refRect.width) < 5;
        const sameHeight = Math.abs(elRect.height - refRect.height) < 5;
        if (sameWidth && sameHeight && elRect.width > 0) score += 10;

        // 4. Child count (indicates structural complexity)
        const sameChildCount = Math.abs(el.children.length - reference.children.length) < 2;
        if (sameChildCount) score += 5;

        // 5. Content characteristics
        const elText = (el.innerText || "").trim();
        const refText = (reference.innerText || "").trim();
        if (Math.abs(elText.length - refText.length) < 20) score += 5;

        // Text prefix match (often contains fixed labels like "Sort by:")
        if (elText.length > 5 && refText.length > 5 && elText.substring(0, 10) === refText.substring(0, 10)) {
            score += 10;
        }

        return score;
    }

    private isXPath(selector: string): boolean {
        return selector.startsWith('/') || selector.startsWith('(') || selector.startsWith('.//');
    }

    /**
     * Scores a selector by quality — higher score = better selector.
     * Semantic/class-based selectors rank high; structural paths rank low.
     */
    private scoreSelectorQuality(sel: string): number {
        let score = 0;
        // Structural path penalty — each > combinator = one fragile level
        score -= (sel.match(/>/g) || []).length * 20;
        // Class selector reward
        if (sel.includes('.')) score += 30;
        // Semantic data-attribute reward
        if (sel.includes('[data-')) score += 40;
        if (sel.includes('[role') || sel.includes('[aria-')) score += 35;
        // :has() child-feature reward (specific but valid)
        if (sel.includes(':has(')) score += 25;
        // XPath slight penalty (valid but less ergonomic)
        if (this.isXPath(sel)) score -= 5;
        // Brevity bonus — short selectors are easier to reason about
        score -= Math.max(0, sel.length - 30) * 0.3;
        return score;
    }

    private findCommonChildFeatures(elements: HTMLElement[]): string[] {
        if (elements.length < 2) return [];

        // Scan descendants for significant classes and data-attributes
        const candidateFeatures = new Map<string, number>();
        const threshold = elements.length * 0.8;
        const childAttrList = ['data-at', 'data-testid', 'data-test-id', 'data-cy', 'itemprop', 'role', 'data-component'];

        elements.forEach(el => {
            const seenInThisItem = new Set<string>();
            const descendants = el.querySelectorAll('*');

            descendants.forEach(d => {
                // 1. Scan for stable child classes (including hash classes via cleanClassesPermissive)
                if (d.className && typeof d.className === 'string') {
                    const classes = this.cleanClassesPermissive(d.className.split(/\s+/));
                    for (const cls of classes) {
                        const selector = `.${cls}`;
                        if (!seenInThisItem.has(selector)) {
                            candidateFeatures.set(selector, (candidateFeatures.get(selector) || 0) + 1);
                            seenInThisItem.add(selector);
                        }
                    }
                }

                // 2. Scan for data-attributes on child elements
                for (const attr of childAttrList) {
                    const val = (d as HTMLElement).getAttribute(attr);
                    if (val) {
                        const safe = val.replace(/"/g, '\\"');
                        const feature = `[${attr}="${safe}"]`;
                        if (!seenInThisItem.has(feature)) {
                            candidateFeatures.set(feature, (candidateFeatures.get(feature) || 0) + 1);
                            seenInThisItem.add(feature);
                        }
                    }
                }
            });
        });

        const result: string[] = [];

        candidateFeatures.forEach((count, feature) => {
            if (count >= threshold) {
                result.push(feature);
            }
        });

        // Limit to top 3 features to avoid explosion
        return result.slice(0, 3);
    }

    /**
     * MAJORITY RULE VALIDATION:
     * Accepts a selector if it covers at least 80% of the target elements.
     * This handles feeds where occasional ads or distinct items break strict 100% patterns.
     */
    private validateSelectorMajority(selector: string, root: HTMLElement, targetCount: number): boolean {
        try {
            let matches = 0;
            if (this.isXPath(selector)) {
                const result = document.evaluate(
                    selector,
                    root,
                    null,
                    XPathResult.ORDERED_NODE_SNAPSHOT_TYPE,
                    null
                );
                matches = result.snapshotLength;
            } else {
                matches = root.querySelectorAll(selector).length;
            }

            // Allow for 20% drop (missing items) or up to 2x growth (extra items like hidden ones or ads)
            // But strict lower bound is critical to ensure we actually got the list.
            return matches >= (targetCount * 0.8) && matches <= (targetCount * 2.0);
        } catch { return false; }
    }

    private findAllCommonClasses(elements: HTMLElement[]): string[] {
        if (elements.length === 0) return [];
        // Use the first element that has classes as the baseline
        // (Sometimes first element might be clean but second isn't, so finding first populated one is better)
        const baseline = elements.find(e => e.className && e.className.length > 0) || elements[0];
        if (!baseline) return [];

        // Use cleanClassesPermissive so hash classes (css-*, sc-*) reach the majority-vote gate
        let intersection = this.cleanClassesPermissive(baseline.className.split(/\s+/));

        // Majority rule for classes too: Class must be present in 80% of items
        const threshold = elements.length * 0.8;

        intersection = intersection.filter(cls => {
            const count = elements.filter(el => {
                // Also use cleanClassesPermissive here to ensure consistent class filtering
                const elClasses = this.cleanClassesPermissive(el.className.split(/\s+/));
                return elClasses.includes(cls);
            }).length;
            return count >= threshold;
        });

        return intersection.map(c => `.${c}`);
    }

    private findAllCommonAttributes(elements: HTMLElement[]): string[] {
        // Expanded list for modern SPAs (LinkedIn, Instagram, Twitter) + site-specific hooks
        const priorityAttributes = [
            'data-testid', 'data-test-id', 'data-component', 'itemprop', 'role', 'data-cy', 'aria-label', 'name',
            'data-urn', 'data-id', 'data-entity-urn', 'data-control-name', 'data-li-id', 'data-index',
            'data-at',     // Nike/Nykaa automation hook
            'data-qa',     // common QA attribute pattern
            'data-type',   // product type/category markers
            'data-item'    // generic item marker
        ];
        const found: string[] = [];
        const threshold = elements.length * 0.8;

        for (const attr of priorityAttributes) {
            // Count how many have this attribute
            const haveAttr = elements.filter(el => el.hasAttribute(attr));

            if (haveAttr.length >= threshold) {
                // Check value consistency
                // Strategy: If specific value is dominant, use it. If not, generic attribute selector.
                const values = haveAttr.map(el => el.getAttribute(attr));

                // Find most common value
                const valueCounts = new Map<string, number>();
                values.forEach(v => {
                    if (v) valueCounts.set(v, (valueCounts.get(v) || 0) + 1);
                });

                let bestValue = '';
                let bestCount = 0;
                valueCounts.forEach((count, val) => {
                    if (count > bestCount) {
                        bestCount = count;
                        bestValue = val;
                    }
                });

                // Use value only if it's stable (not an ID, numeric, or instance-specific)
                if (bestCount >= threshold && bestValue && isStableAttributeValue(attr, bestValue)) {
                    const safeValue = bestValue.replace(/"/g, '\\"');
                    found.push(`[${attr}="${safeValue}"]`);
                } else {
                    // Value is instance-specific or non-dominant — use existence selector only
                    found.push(`[${attr}]`);
                }
            }
        }
        return found;
    }

    private findCommonTag(elements: HTMLElement[]): string | null {
        if (elements.length === 0) return null;

        const counts = new Map<string, number>();
        elements.forEach(el => {
            const tag = el.tagName.toLowerCase();
            counts.set(tag, (counts.get(tag) || 0) + 1);
        });

        // Find dominant tag
        let bestTag = '';
        let max = 0;
        counts.forEach((v, k) => {
            if (v > max) {
                max = v;
                bestTag = k;
            }
        });

        if (max >= elements.length * 0.8) {
            return bestTag;
        }
        return null;
    }

    /**
     * Allows CSS-in-JS hash classes through while still filtering Tailwind utilities.
     * Use where majority-vote is the gate (findAllCommonClasses).
     * Hash classes like css-dy3i81 or sc-abc12 are deterministic per build,
     * so if they're shared by 80%+ of siblings, they're structurally meaningful.
     */
    private cleanClassesPermissive(classes: string[]): string[] {
        return classes.filter(c => c && (!isUnstableClass(c) || isHashClass(c)));
    }
}
