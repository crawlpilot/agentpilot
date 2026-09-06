import {
    getStructuralFingerprint,
    calculateSimilarityScore,
} from '../../utils/similarity';
import type { StructuralFingerprint } from '../../utils/similarity';

/**
 * Logic for refining list items within a detected container.
 * This ensures we select the individual "cards" or "rows" even when the 
 * ContainerDetector has expanded to a larger grid.
 */
export class ItemSelectorRefiner {
    /**
     * Finds the primary repeating element (the "Item") that contains the target.
     * It looks for the largest block that is still repeating within the container.
     */
    public findTargetItem(
        target: HTMLElement,
        container: HTMLElement,
        detectedSiblings?: HTMLElement[]
    ): HTMLElement {
        // Quick path: if target is one of the detected siblings, use it
        if (detectedSiblings && detectedSiblings.includes(target)) {
            return target;
        }

        const targetFingerprint = getStructuralFingerprint(target);
        let current: HTMLElement | null = target;
        let bestItem: HTMLElement = target;
        let maxCount = 0;

        // Climb up from target until we hit the container
        while (current && current !== container && current !== document.body) {
            const parentElement: HTMLElement | null = current.parentElement;
            if (!parentElement) break;

            // Shortcut: checking if this ancestor is a known sibling
            if (detectedSiblings && detectedSiblings.includes(current)) {
                return current;
            }

            // Check if this level is a repeating unit
            // We climb as HIGH as possible as long as it still repeats.
            const similarCount = parentElement === container
                ? this.countSimilar(current, container, targetFingerprint)
                : this.countSimilar(current, parentElement, targetFingerprint);

            // Heuristic for picking the "best" level:
            // 1. If this level has many more repeating siblings than the previous best, it's likely better.
            // 2. If it has similar count (or at least 2), and it's higher, we usually prefer higher (the "card" over the "image").
            // 3. BUT if the count drops drastically (e.g. from 20 items to 2 sections), we might want to stay at the item level.
            if (similarCount >= 2) {
                // If count is at least 70% of maxCount, we prefer higher level
                if (similarCount >= maxCount * 0.7) {
                    bestItem = current;
                    maxCount = Math.max(maxCount, similarCount);
                } else if (maxCount === 0) {
                    bestItem = current;
                    maxCount = similarCount;
                }
            }

            current = parentElement;
        }

        return bestItem;
    }

    private countSimilar(target: HTMLElement, parent: HTMLElement, targetData?: StructuralFingerprint): number {
        const data = targetData ?? getStructuralFingerprint(target);
        return (Array.from(parent.children) as HTMLElement[]).filter(child =>
            calculateSimilarityScore(data, getStructuralFingerprint(child)) >= 35
        ).length;
    }
}
