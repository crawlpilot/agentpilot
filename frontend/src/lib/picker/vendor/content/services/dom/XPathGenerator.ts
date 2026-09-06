
/**
 * Utility class for generating robust XPath selectors.
 * Focuses on semantic stability (IDs, text, attributes) rather than just structure.
 */
export class XPathGenerator {

    /**
     * Generates a list of robust XPath selectors for a given element.
     * Prioritizes semantic attributes, text content, and stable classes.
     */
    public static generate(element: HTMLElement): Array<{ value: string, quality: number, type: string }> {
        const results: Array<{ value: string, quality: number, type: string }> = [];
        const tag = element.tagName.toLowerCase();

        // 1. Exact ID
        // [CHANGED] Stricter check for dynamic IDs
        if (element.id && !/\d/.test(element.id) && !/(Instance|ember|react|guid|uid|_)/i.test(element.id) && element.id.length < 40) {
            results.push({ value: `//*[@id="${element.id}"]`, quality: 100, type: 'ID' });
        }

        // 2. Semantic Text Content (High Priority for Buttons/Links)
        this.addTextSelectors(element, tag, results);

        // 3. Semantic Attributes (rel, aria-label, etc.)
        this.addAttributeSelectors(element, tag, results);

        // 4. Class Combinations
        this.addClassSelectors(element, tag, results);
        this.addClassCombinationSelectors(element, tag, results);

        // 5. Hierarchy (Parent Context)
        this.addParentDescendantSelectors(element, tag, results);

        // 6. Sibling Context (Previous label/header)
        this.addSiblingContextSelectors(element, tag, results);

        const uniqueResults: { value: string, quality: number, type: string }[] = [];

        results.forEach(r => {
            if (this.isUnique(r.value)) {
                uniqueResults.push(r);
            } else {
                // Selector matches multiple items.
                // 1. Try 'last()' - often the most relevant for pagination/lists (bottom of page)
                const lastXPath = `(${r.value})[last()]`;
                if (this.isUnique(lastXPath)) {
                    uniqueResults.push({
                        value: lastXPath,
                        quality: r.quality - 2, // Very slight penalty, last() is usually safe
                        type: `${r.type} (Last)`
                    });
                }

                // 2. Try '1' - Top of page
                const firstXPath = `(${r.value})[1]`;
                if (this.isUnique(firstXPath)) {
                    uniqueResults.push({
                        value: firstXPath,
                        quality: r.quality - 5, // Higher penalty than last() as top items can be hidden
                        type: `${r.type} (First)`
                    });
                }
            }
        });

        return uniqueResults.sort((a, b) => b.quality - a.quality);
    }

    private static addTextSelectors(element: HTMLElement, tag: string, results: any[]) {
        const text = element.textContent?.trim() || ''; // Use textContent to get all text
        // Limit text length to avoid fragile selectors on long paragraphs
        if (text && text.length > 0 && text.length < 50) { // increased limit slightly to cover verbose buttons
            // Escape single quotes for XPath
            const cleanText = text.replace(/'/g, "\\'");

            // 1. Exact match using normalize-space(string())
            // string() converts the context node to string value, matching nested text which text() does not
            results.push({
                value: `//${tag}[normalize-space(string())='${cleanText}']`,
                quality: 95, // High quality, exact match
                type: 'Text Exact'
            });

            // 2. Contains match for Any short text (not just keywords)
            // This mirrors the "contains string XPath (most robust)" from reference
            if (text.length > 3) {
                results.push({
                    value: `//${tag}[contains(string(), '${cleanText}')]`,
                    quality: 90, // High quality, robust against whitespace/nesting
                    type: 'Text Contains'
                });
            }

            // 3. Keyword Bonus (Prioritize slightly higher if it matches known pagination keywords)
            if (['next', 'more', 'load', 'previous', 'prev', '>', '»', 'arrow', 'right'].some(k => text.toLowerCase().includes(k))) {
                // We already added the selectors above, but we can add a specific one with slightly hacked quality
                // OR we just rely on the above.
                // Let's ensure we have a fallback for partial keyword match if exact fails
                // e.g. text is "Next Page >" and we just want to match "Next"

                const keywords = ['Next', 'next', 'More', 'more', 'Show more', 'Load more'];
                keywords.forEach(kw => {
                    if (text.includes(kw)) {
                        results.push({
                            value: `//${tag}[contains(string(), '${kw}')]`,
                            quality: 88,
                            type: 'Keyword Partial'
                        });
                    }
                });
            }
        }
    }

    private static addAttributeSelectors(element: HTMLElement, tag: string, results: any[]) {
        // Rel="next" (Golden standard for pagination)
        if (element.getAttribute('rel') === 'next') {
            results.push({ value: `//${tag}[@rel='next']`, quality: 95, type: 'Attribute (rel)' });
        }

        // Aria Label
        const ariaLabel = element.getAttribute('aria-label');
        if (ariaLabel) {
            results.push({ value: `//${tag}[@aria-label='${ariaLabel}']`, quality: 80, type: 'Attribute (aria)' });
        }

        // Title
        const title = element.getAttribute('title');
        if (title) {
            results.push({ value: `//${tag}[@title='${title}']`, quality: 80, type: 'Attribute (title)' });
        }
    }

    private static addClassSelectors(element: HTMLElement, tag: string, results: any[]) {
        if (!element.className || typeof element.className !== 'string') return;

        const classes = element.className.split(/\s+/).filter(c => c.trim().length > 0);

        // Check for pagination specific classes
        const paginationClasses = classes.filter(c => {
            const lower = c.toLowerCase();
            return lower.includes('pagination') || lower.includes('next') || lower.includes('page');
        });

        // Specific class contains selector
        // //a[contains(@class, "pagination-next")]
        paginationClasses.forEach(cls => {
            results.push({
                value: `//${tag}[contains(@class, '${cls}')]`,
                quality: 70, // Bit lower than ID/Text but better than generic path
                type: 'Class Contains'
            });
        });

        // Combined meaningful classes
        if (classes.length > 0) {
            const classCondition = classes.map(c => `contains(@class, '${c}')`).join(' and ');
            // Only if not too many classes
            if (classes.length <= 3) {
                results.push({
                    value: `//${tag}[${classCondition}]`,
                    quality: 60,
                    type: 'Classes Combined'
                });
            }
        }
    }

    private static addClassCombinationSelectors(element: HTMLElement, tag: string, results: any[]) {
        if (!element.className || typeof element.className !== 'string') return;
        const classes = element.className.split(/\s+/).filter(c => c.trim().length > 0 && !c.match(/\d/)); // Ignore classes with numbers

        // Two-class combinations
        if (classes.length >= 2) {
            for (let i = 0; i < classes.length; i++) {
                for (let j = i + 1; j < classes.length; j++) {
                    const c1 = classes[i];
                    const c2 = classes[j];
                    results.push({
                        value: `//${tag}[contains(@class, '${c1}') and contains(@class, '${c2}')]`,
                        quality: 75,
                        type: 'Classes Combined (Pair)'
                    });
                }
            }
        }
    }

    private static addParentDescendantSelectors(element: HTMLElement, tag: string, results: any[]) {
        let parent = element.parentElement;
        let depth = 0;

        while (parent && parent !== document.body && depth < 3) {
            // Parent ID
            if (parent.id && !/\d/.test(parent.id) && !/(Instance|ember|react|guid|uid|_)/i.test(parent.id)) {
                results.push({
                    value: `//*[@id="${parent.id}"]//${tag}`,
                    quality: 80, // Better than raw structure, worse than direct ID
                    type: 'Parent ID Descendant'
                });
            }

            // Parent Class (if specific)
            if (parent.className && typeof parent.className === 'string') {
                const pClasses = parent.className.split(/\s+/).filter(c => c.includes('container') || c.includes('wrapper') || c.includes('section') || c.includes('list'));
                pClasses.forEach(pc => {
                    results.push({
                        value: `//${parent!.tagName.toLowerCase()}[contains(@class, '${pc}')]//${tag}`,
                        quality: 60,
                        type: 'Parent Class Descendant'
                    });
                });
            }

            parent = parent.parentElement;
            depth++;
        }
    }

    private static addSiblingContextSelectors(element: HTMLElement, tag: string, results: any[]) {
        // Look for preceding sibling that might be a label
        let sibling = element.previousElementSibling;
        while (sibling) {
            if (sibling.tagName === 'LABEL' || sibling.tagName === 'H3' || sibling.tagName === 'H4' || (sibling.textContent && sibling.textContent.length < 20)) {
                const text = sibling.textContent?.trim();
                if (text) {
                    const cleanText = text.replace(/'/g, "\\'");
                    results.push({
                        value: `//${sibling.tagName.toLowerCase()}[contains(string(), '${cleanText}')]/following-sibling::${tag}`,
                        quality: 70,
                        type: 'Sibling Context'
                    });
                    break; // Only use immediate or close context
                }
            }
            sibling = sibling.previousElementSibling;
        }
    }

    private static isUnique(xpath: string): boolean {
        try {
            const res = document.evaluate(xpath, document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
            return res.snapshotLength === 1;
        } catch (e) {
            return false;
        }
    }
}
