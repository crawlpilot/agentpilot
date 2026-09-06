import { calculateSimilarityScore, getStructuralFingerprint } from '../../utils/similarity';
import { hasRenderedBox } from '../../utils/rendering';
import { VisualBlockExtractor } from './VisualBlockExtractor';
import type { IContainerDetector, ContainerResult } from './IContainerDetector';

interface ContainerCandidate {
    container: HTMLElement;
    siblings: HTMLElement[];
    childCount: number;
    confidence: number;
    level: number;
    area: number;
}

/**
 * FingerprintContainerDetector — the original implementation.
 *
 * Uses structural fingerprinting and similarity scoring to find list containers.
 * Works by computing a "fingerprint" of the hovered element, then searching
 * siblings at each ancestor level for elements with a similar fingerprint.
 *
 * Strengths: Precise for homogeneous lists with consistent DOM structure.
 * Weaknesses: Fragile when hovering deeply nested elements (e.g. <img> inside a card),
 *             because the fingerprint of the inner element won't match the card siblings.
 */
export class FingerprintContainerDetector implements IContainerDetector {
    private maxLevelsUp = 8;
    private minSiblings = 2;
    private visualExtractor: VisualBlockExtractor;

    constructor() {
        this.visualExtractor = new VisualBlockExtractor();
    }

    public findContainer(element: HTMLElement): ContainerResult {
        console.log('[FingerprintContainerDetector] findContainer for:', element);

        const candidates: ContainerCandidate[] = [];
        let current: HTMLElement | null = element;

        // Traverse up to find potential containers
        for (let level = 0; level < this.maxLevelsUp; level++) {
            if (!current || current === document.body || current === document.documentElement) break;

            const parent: HTMLElement | null = current.parentElement;
            if (!parent) break;

            const cluster = this.findClusterInParent(element, parent);
            console.log(`[FingerprintContainerDetector] Level ${level} (Parent: ${parent.tagName}.${parent.className}): Cluster size = ${cluster.length}`);

            if (cluster.length >= this.minSiblings) {
                const similarity = this.calculateClusterScore(parent, cluster);
                console.log(`[FingerprintContainerDetector] -> Candidate Valid! Score: ${similarity}`);

                candidates.push({
                    container: parent,
                    siblings: cluster,
                    childCount: cluster.length,
                    confidence: similarity,
                    level: level,
                    area: parent.offsetWidth * parent.offsetHeight
                });
            }

            current = parent;
        }

        if (candidates.length === 0) {
            console.log('[FingerprintContainerDetector] No candidates found.');
            return { isContainer: false, container: null, childCount: 0, siblings: [], confidence: 0 };
        }

        candidates.sort((a, b) => {
            if (Math.abs(a.confidence - b.confidence) > 15) {
                return b.confidence - a.confidence;
            }
            if (a.childCount > b.childCount * 1.5) return -1;
            if (b.childCount > a.childCount * 1.5) return 1;
            return a.area - b.area;
        });

        console.log('[FingerprintContainerDetector] Sorted Candidates:', candidates);

        const best = candidates[0];

        return {
            isContainer: true,
            container: best.container,
            childCount: best.childCount,
            siblings: best.siblings,
            confidence: best.confidence,
            candidates: candidates
        };
    }

    /**
     * Finds elements within 'parent' that are structurally similar to 'target'.
     */
    private findClusterInParent(target: HTMLElement, parent: HTMLElement): HTMLElement[] {
        const children = Array.from(parent.children) as HTMLElement[];

        const targetAncestorIndex = children.findIndex(c => c === target || c.contains(target));
        if (targetAncestorIndex === -1) return [];

        const seedNode = children[targetAncestorIndex];
        const seedFP = getStructuralFingerprint(seedNode);

        const cluster: HTMLElement[] = [];

        children.forEach(child => {
            if (!hasRenderedBox(child)) return;

            if (child === seedNode) {
                cluster.push(child);
                return;
            }

            const childFP = getStructuralFingerprint(child);
            const score = calculateSimilarityScore(seedFP, childFP);

            if (score >= 50) {
                cluster.push(child);
            }
        });

        if (cluster.length > 1) {
            return cluster.filter(c => this.visualExtractor.areVisuallyAligned(seedNode, c));
        }

        return cluster;
    }

    private calculateClusterScore(container: HTMLElement, cluster: HTMLElement[]): number {
        const validChildrenCount = Array.from(container.children).filter(c =>
            hasRenderedBox(c as HTMLElement)
        ).length;

        const coverage = cluster.length / (validChildrenCount || 1);
        const baseScore = 80;
        return Math.min(100, baseScore + (coverage * 20));
    }

    public findInternalContainer(_element: HTMLElement): ContainerResult {
        console.log('[FingerprintContainerDetector] findInternalContainer for:', _element);
        // Fingerprint-based internal detection is a future enhancement.
        return { isContainer: false, container: null, childCount: 0, siblings: [], confidence: 0 };
    }
}
