export interface VisualBlock {
    element: HTMLElement;
    rect: DOMRect;
    isSeparator: boolean;
}

/**
 * VisualBlockExtractor - Simple VIPS (Vision-based Page Segmentation) Lite
 * Identifies visual separators and distinct blocks using computed styles and dimensions.
 */
export class VisualBlockExtractor {

    /**
     * Checks if an element acts as a visual separator (e.g. line, gap).
     */
    public isVisualSeparator(el: HTMLElement): boolean {
        // 1. Explicit Separators
        if (el.tagName === 'HR') return true;

        // 2. Computed Style Borders
        const style = window.getComputedStyle(el);
        if (style.borderBottomWidth && parseFloat(style.borderBottomWidth) > 0) return true;
        if (style.borderTopWidth && parseFloat(style.borderTopWidth) > 0) return true;

        // 3. Dimensional Separators (Thin and wide, or tall and narrow)
        // Only consider if it has no meaningful text content
        if (el.innerText.trim().length === 0) {
            const rect = el.getBoundingClientRect();
            // Horizontal Line
            if (rect.height < 5 && rect.width > 50) return true;
            // Vertical Line
            if (rect.width < 5 && rect.height > 50) return true;
        }

        return false;
    }

    /**
     * Extracts visually distinct blocks from a root element.
     * Use this to find potential "Cards" that are visually separated.
     */
    public getVisualBlocks(root: HTMLElement): VisualBlock[] {
        const blocks: VisualBlock[] = [];

        // Flatten children for analysis
        const children = Array.from(root.children) as HTMLElement[];

        children.forEach(child => {
            const rect = child.getBoundingClientRect();

            // Skip invisible elements
            if (rect.width === 0 || rect.height === 0 || window.getComputedStyle(child).display === 'none') {
                return;
            }

            const isSep = this.isVisualSeparator(child);

            // If it's a separator, mark it
            if (isSep) {
                blocks.push({ element: child, rect, isSeparator: true });
                return;
            }

            // Otherwise it's a content block
            // Heuristic: If it has significant size, it's a block
            if (rect.width > 20 && rect.height > 20) {
                blocks.push({ element: child, rect, isSeparator: false });
            }
        });

        return blocks;
    }

    /**
     * Checks if two elements are horizontally or vertically aligned in a grid/list pattern.
     */
    public areVisuallyAligned(a: HTMLElement, b: HTMLElement): boolean {
        const r1 = a.getBoundingClientRect();
        const r2 = b.getBoundingClientRect();

        // 1. Strict Alignment (Row/Column)
        const topAlign = Math.abs(r1.top - r2.top) < 5;
        const leftAlign = Math.abs(r1.left - r2.left) < 5;

        if (topAlign || leftAlign) return true;

        // [REFINED] Grid/Flow Layout Support (Dimension Matching)
        // If elements are not aligned (e.g. diagonal in a grid), check if they have similar size.
        // 5px tolerance is too tight for variable content.
        // For lists, we often care more about matching one dimension (width) than both perfectly.
        const widthDiff = Math.abs(r1.width - r2.width);
        const heightDiff = Math.abs(r1.height - r2.height);

        const similarWidth = widthDiff < 10 || (widthDiff / Math.max(r1.width, 1)) < 0.1;
        const similarHeight = heightDiff < 10 || (heightDiff / Math.max(r1.height, 1)) < 0.4; // [RELAXED]

        // 1D Alignment: If width matches perfectly (common in vertical lists), height can vary significantly.
        if (similarWidth && (widthDiff < 5 || (widthDiff / r1.width) < 0.05)) return true;

        return similarWidth && similarHeight;
    }
}
