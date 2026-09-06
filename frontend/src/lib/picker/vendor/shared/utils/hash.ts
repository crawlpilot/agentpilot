/**
 * Deterministic 32-bit string hash (DJB2-style variant). This exact loop was copy-pasted
 * across 7 files for row/element dedup and page-change detection — consolidated here so
 * there's one implementation to test and reason about. Callers apply their own
 * post-processing (toString(), base36, prefixing) on top of the raw signed int.
 */
export function hashString(str: string): number {
    let hash = 0;
    for (let i = 0; i < str.length; i++) {
        hash = ((hash << 5) - hash) + str.charCodeAt(i);
        hash |= 0; // Convert to 32-bit integer
    }
    return hash;
}
