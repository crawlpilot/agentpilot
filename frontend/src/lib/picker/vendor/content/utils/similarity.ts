// Enhanced Similarity Logic with Scoring Maps and Visual Signals

export interface StructureNode {
    tag: string;
    depth: number;
    visualSignal?: string; // Binary string "1010" representing visible children
}

export interface StructuralFingerprint {
    tagName: string;
    id: string;
    className: string;
    dimensions: {
        width: number;
        height: number;
    };
    childCount: number;
    // FR-2: Scoring Map (Structure by Depth)
    structureMap: StructureNode[];
    // FR-1: Flattened representation for quick clustering
    visualSignalVector: string;
    textLength: number;
}

/**
 * Generates a comprehensive structural fingerprint for an element.
 */
export function getStructuralFingerprint(el: HTMLElement): StructuralFingerprint {
    const rect = el.getBoundingClientRect();
    const structureMap: StructureNode[] = [];

    // Depth-limited traversal for Scoring Map (Max depth 3 for performance)
    traverseStructure(el, 0, structureMap, 3);

    // Generate Visual Signal Vector (simplified: direct children visibility)
    // 1 = Element, 0 = Text/Comment/Hidden
    const visualSignalVector = Array.from(el.childNodes).map(n => {
        if (n.nodeType === Node.ELEMENT_NODE) {
            const e = n as HTMLElement;
            return (e.offsetWidth > 0 && e.offsetHeight > 0) ? '1' : '0';
        }
        return '0';
    }).join('');

    return {
        tagName: el.tagName.toLowerCase(),
        id: el.id || "",
        className: getClassPattern(el),
        dimensions: {
            width: rect.width,
            height: rect.height
        },
        childCount: el.children.length,
        structureMap: structureMap,
        visualSignalVector: visualSignalVector,
        textLength: (el.textContent || "").length,
    };
}

function traverseStructure(el: HTMLElement, currentDepth: number, map: StructureNode[], maxDepth: number) {
    if (currentDepth > maxDepth) return;

    map.push({
        tag: el.tagName.toLowerCase(),
        depth: currentDepth
    });

    Array.from(el.children).forEach(child => {
        traverseStructure(child as HTMLElement, currentDepth + 1, map, maxDepth);
    });
}

export function getClassPattern(element: HTMLElement): string {
    if (!element.className || typeof element.className !== 'string') return '';
    return element.className.split(/\s+/)
        .filter(isSignificantClass)
        .sort()
        .join(' ');
}

function isSignificantClass(c: string): boolean {
    if (c.trim().length === 0) return false;
    const badPatterns = ['active', 'selected', 'focus', 'hover', 'current', 'highlight', 'ng-', 'v-', 'react-', 'emotion-', 'css-'];
    if (badPatterns.some(bad => c.toLowerCase().includes(bad))) return false;
    // Filter out purely numeric classes (random hashes/layout)
    if (/^\d+$/.test(c)) return false;
    // Filter out typical Tailwind spacing/layout classes if they are too generic? 
    // Actually, for structure, layout classes ARE significant.
    return true;
}

/**
 * Calculates similarity score (0-100) between two fingerprints using Hybrid Structural Scoring.
 */
export function calculateSimilarityScore(a: StructuralFingerprint, b: StructuralFingerprint): number {
    let score = 0;

    // 1. Tag Name Match (Base Requirement? No, but high weight) (+15)
    if (a.tagName === b.tagName) score += 15;

    // 2. Class Name Similarity (+20)
    // Jaccard Index of classes
    const aClasses = a.className.split(' ');
    const bClasses = b.className.split(' ');
    if (aClasses.length > 0 || bClasses.length > 0) {
        const setA = new Set(aClasses);
        const setB = new Set(bClasses);
        let intersection = 0;
        setA.forEach(c => { if (setB.has(c)) intersection++; });
        const union = new Set([...aClasses, ...bClasses]).size;

        if (union > 0) {
            score += (intersection / union) * 20;
        } else {
            score += 20; // Both empty classes - match
        }
    } else {
        score += 20; // Both no classes
    }

    // 3. Visual Signal Vector Similarity (+15) (FR-1)
    // Levenshtein distance normalized? Or simple equality?
    // Let's use simple match for now, maybe normalized Levenshtein later if needed.
    if (a.visualSignalVector === b.visualSignalVector) {
        score += 15;
    } else {
        // Partial match
        const dist = levenshtein(a.visualSignalVector, b.visualSignalVector);
        const maxLen = Math.max(a.visualSignalVector.length, b.visualSignalVector.length);
        if (maxLen > 0) {
            score += (1 - (dist / maxLen)) * 15;
        }
    }

    // 4. Scoring Map / Inner Structure (+35) (FR-2)
    // Compare structural depth logs
    const structureScore = compareStructureMaps(a.structureMap, b.structureMap);
    score += structureScore * 35;


    // 5. Dimensions (+15)
    // Aspect Ratio similarity is often better than absolute dimensions for responsive layouts
    const aRatio = a.dimensions.width / (a.dimensions.height || 1);
    const bRatio = b.dimensions.width / (b.dimensions.height || 1);
    const ratioDiff = Math.abs(aRatio - bRatio);

    if (ratioDiff < 0.1) score += 15;
    else if (ratioDiff < 0.5) score += 5;

    return Math.min(score, 100);
}

function compareStructureMaps(mapA: StructureNode[], mapB: StructureNode[]): number {
    // Quick heuristic: Align by depth
    // Ideally use Tree Edit Distance, but for performance, we compare "bag of tags at depth X"
    const maxNodes = Math.max(mapA.length, mapB.length);
    if (maxNodes === 0) return 1;

    // We can't do strict index matching because one might have an extra wrapper.
    // Let's simplified Jaccard on (Tag + Depth) keys
    const setA = new Set(mapA.map(n => `${n.depth}:${n.tag}`));
    const setB = new Set(mapB.map(n => `${n.depth}:${n.tag}`));

    let intersection = 0;
    setA.forEach(k => { if (setB.has(k)) intersection++; });
    const union = new Set([...setA, ...setB]).size;

    return intersection / union;
}

// Helper: Levenshtein Distance for strings
function levenshtein(a: string, b: string): number {
    if (a.length === 0) return b.length;
    if (b.length === 0) return a.length;
    const matrix = [];
    for (let i = 0; i <= b.length; i++) { matrix[i] = [i]; }
    for (let j = 0; j <= a.length; j++) { matrix[0][j] = j; }
    for (let i = 1; i <= b.length; i++) {
        for (let j = 1; j <= a.length; j++) {
            if (b.charAt(i - 1) === a.charAt(j - 1)) {
                matrix[i][j] = matrix[i - 1][j - 1];
            } else {
                matrix[i][j] = Math.min(matrix[i - 1][j - 1] + 1, Math.min(matrix[i][j - 1] + 1, matrix[i - 1][j] + 1));
            }
        }
    }
    return matrix[b.length][a.length];
}
