import type { ISelectionStrategy } from './ISelectionStrategy';
import { VisualElementPicker } from '../VisualElementPicker';
import { DataExtractor } from '../../../services/extraction/DataExtractor';
import { ContainerDetector } from '../ContainerDetector';
import type { ContainerResult } from '../ContainerDetector';
import { generateCssSelector, generateXPath, generateRobustSelectors, clickElementRobust } from '../../../services/dom/domUtils';
import type { SelectorResult } from '../../../services/dom/selectors/SelectorGenerator';
import { hasRenderedBox } from '../../../utils/rendering';

function bestCss(results: SelectorResult[], fallback: () => string): string {
    return results.find(r => !r.selector.startsWith('/') && !r.selector.startsWith('(') && !r.selector.startsWith('./'))?.selector ?? fallback();
}
function bestXPath(results: SelectorResult[], fallback: () => string): string {
    return results.find(r => r.selector.startsWith('/') || r.selector.startsWith('(') || r.selector.startsWith('./'))?.selector ?? fallback();
}

type ExtractionType = 'text' | 'link' | 'image' | 'list' | 'table' | 'image_array' | 'text_array' | 'link_array';

interface ClassificationResult {
    extractionType: ExtractionType;
    previewValue: string;
    finalValue: string;
    containerSelector?: string;
    containerXPath?: string;
    itemSelector?: string;
    itemXPath?: string;
    count?: number;
}

export class DetailSelectionStrategy implements ISelectionStrategy {
    private extractor: DataExtractor;
    private detector: ContainerDetector;

    public preferredAction?: 'extract' | 'click' = 'extract';

    constructor() {
        this.extractor = new DataExtractor();
        this.detector = new ContainerDetector();
    }

    handleHover(element: HTMLElement, picker: VisualElementPicker): void {
        picker.updateHighlightManual(
            element,
            'Select Element',
            'Click to edit & extract',
            undefined,
            []
        );
    }

    handleClick(element: HTMLElement, picker: VisualElementPicker, sendResponse: (msg: any) => void): void {
        console.log('Detail Strategy Clicked:', element);

        const target = element;

        // Detect internal container (list/table)
        const containerInfo = this.detector.findInternalContainer(target);
        const isList = !!(containerInfo.isContainer && containerInfo.container);

        // Build selectors and extract data
        let containerSelector: string;
        let containerXPath: string;
        let itemSelector: string;
        let itemXPath: string;
        let data: any;

        if (isList) {
            console.log('[DetailStrategy] Detected list/table container:', containerInfo);
            const cSelectors = generateRobustSelectors(containerInfo.container!);
            containerSelector = bestCss(cSelectors, () => generateCssSelector(containerInfo.container!));
            containerXPath = bestXPath(cSelectors, () => generateXPath(containerInfo.container!));
            const iSelectors = generateRobustSelectors(target);
            itemSelector = bestCss(iSelectors, () => generateCssSelector(target, false));
            itemXPath = bestXPath(iSelectors, () => generateXPath(target));
            data = this.extractor.extract(target, containerInfo);
        } else {
            console.log('[DetailStrategy] No list detected. Extracting single element.');
            const tSelectors = generateRobustSelectors(target);
            containerSelector = bestCss(tSelectors, () => generateCssSelector(target));
            containerXPath = bestXPath(tSelectors, () => generateXPath(target));
            itemSelector = containerSelector;
            itemXPath = containerXPath;
            data = this.extractor.extract(target, { isContainer: false, siblings: [], container: null });
        }

        // Classify the element type using ordered detector chain
        const classification = this.classify(element, target, isList, containerInfo, data);

        // Apply any selector overrides from classification (e.g. sibling-table retargets parent)
        if (classification.containerSelector) containerSelector = classification.containerSelector;
        if (classification.containerXPath) containerXPath = classification.containerXPath;
        if (classification.itemSelector) itemSelector = classification.itemSelector;
        if (classification.itemXPath) itemXPath = classification.itemXPath;

        sendResponse({
            type: 'ELEMENT_SELECTED',
            payload: {
                success: true,
                id: `sel-${Date.now()}`,
                data,
                containerSelector,
                containerXPath,
                itemSelector,
                itemXPath,
                value: classification.finalValue,
                previewValue: classification.previewValue,
                tagName: element.tagName.toLowerCase(),
                selectionMode: 'detail',
                action: this.preferredAction || 'extract',
                extractionType: classification.extractionType,
                waitTime: 0,
                patternFound: isList,
                count: classification.count ?? (isList ? (containerInfo.childCount || data?.items?.length || 1) : 1),
                viewportWidth: window.outerWidth,
                viewportHeight: window.outerHeight
            }
        });

        picker.deactivate();

        if (this.preferredAction === 'click') {
            setTimeout(async () => { await clickElementRobust(element); }, 100);
        }
    }

    // =========================================================================
    // Classification Chain — ordered detectors, first match wins
    // =========================================================================

    private classify(
        element: HTMLElement,
        target: HTMLElement,
        isList: boolean,
        containerInfo: ContainerResult,
        data: any
    ): ClassificationResult {
        return (
            this.classifyAsContainer(isList, containerInfo, data) ??
            this.classifyAsHomogeneousArray(target) ??
            this.classifyAsDirectTag(element) ??
            this.classifyAsWrapperContent(element) ??
            this.classifyAsSiblingTable(element) ??
            this.classifyAsText(element)
        );
    }

    /** Detected as list/table container by ContainerDetector */
    private classifyAsContainer(isList: boolean, containerInfo: ContainerResult, data: any): ClassificationResult | null {
        if (!isList) return null;
        const itemCount = containerInfo.childCount || data?.items?.length || 0;
        const extractionType: ExtractionType = containerInfo.containerType === 'table' ? 'table' : 'list';
        const typeStr = extractionType === 'table' ? 'Table' : 'List';
        return {
            extractionType,
            previewValue: `${typeStr} (${itemCount} items)`,
            finalValue: `${typeStr} (${itemCount} items)`,
            count: itemCount
        };
    }

    /** Homogeneous array of same-type children (images, links, icons, etc.) */
    private classifyAsHomogeneousArray(target: HTMLElement): ClassificationResult | null {
        const result = this.detectHomogeneousArray(target);
        if (!result) return null;
        return {
            extractionType: result.arrayType,
            previewValue: `Array (${result.count} items)`,
            finalValue: JSON.stringify(result.values),
            count: result.count
        };
    }

    /** Direct IMG or A tag clicked */
    private classifyAsDirectTag(element: HTMLElement): ClassificationResult | null {
        if (element.tagName === 'IMG') {
            const src = (element as HTMLImageElement).src;
            return { extractionType: 'image', previewValue: src, finalValue: src };
        }
        if (element.tagName === 'A') {
            const href = (element as HTMLAnchorElement).href;
            return { extractionType: 'link', previewValue: href, finalValue: href };
        }
        return null;
    }

    /** Wrapper containing a single dominant image, link, or background-image */
    private classifyAsWrapperContent(element: HTMLElement): ClassificationResult | null {
        const images = element.querySelectorAll('img');
        const textContent = (element.textContent || '').trim();

        // Single dominant image with minimal text
        if (images.length === 1 && textContent.length < 20) {
            const src = (images[0] as HTMLImageElement).src;
            return { extractionType: 'image', previewValue: src, finalValue: src };
        }

        // Single dominant link — must be the only real anchor in the entire subtree
        const anchors = element.querySelectorAll('a[href]:not([href^="javascript:"])');
        if (anchors.length === 1) {
            const anchor = anchors[0] as HTMLAnchorElement;
            if (anchor?.href) {
                return { extractionType: 'link', previewValue: anchor.href, finalValue: anchor.href };
            }
        }

        // Background image
        try {
            const bgImage = window.getComputedStyle(element).backgroundImage;
            if (bgImage && bgImage !== 'none' && bgImage.startsWith('url(')) {
                const url = bgImage.replace(/^url\(["']?/, '').replace(/["']?\)$/, '');
                return { extractionType: 'image', previewValue: url, finalValue: url };
            }
        } catch (_) { /* ignore */ }

        return null;
    }

    /** Element is part of a repeating sibling pattern → treat as table */
    private classifyAsSiblingTable(element: HTMLElement): ClassificationResult | null {
        const parent = element.parentElement;
        if (!parent || parent === document.body || parent === document.documentElement) return null;

        const siblingTag = element.tagName;
        const childCount = element.children.length;
        if (childCount < 2) return null;

        const siblings = Array.from(parent.children) as HTMLElement[];
        const matchingSiblings = siblings.filter(sib =>
            sib.tagName === siblingTag &&
            Math.abs(sib.children.length - childCount) <= 1 &&
            hasRenderedBox(sib) &&
            (sib.textContent || '').trim().length > 0
        );

        if (matchingSiblings.length < 3) return null;

        const pSelectors = generateRobustSelectors(parent);
        const pCss = bestCss(pSelectors, () => generateCssSelector(parent));
        const pXPath = bestXPath(pSelectors, () => generateXPath(parent));
        return {
            extractionType: 'table',
            previewValue: `Table (${matchingSiblings.length} items)`,
            finalValue: `Table (${matchingSiblings.length} items)`,
            containerSelector: pCss,
            containerXPath: pXPath,
            itemSelector: pCss,
            itemXPath: pXPath,
            count: matchingSiblings.length
        };
    }

    /** Default fallback: plain text extraction */
    private classifyAsText(element: HTMLElement): ClassificationResult {
        const textContent = (element.textContent || '').trim();
        return {
            extractionType: 'text',
            previewValue: textContent.substring(0, 50) || '',
            finalValue: textContent || ''
        };
    }

    // =========================================================================
    // Homogeneous Array Detection
    // =========================================================================

    private detectHomogeneousArray(element: HTMLElement): { arrayType: 'image_array' | 'text_array' | 'link_array'; values: string[]; count: number } | null {
        const skipTags = new Set(['BR', 'HR', 'SCRIPT', 'STYLE', 'LINK', 'META']);
        const children = Array.from(element.children).filter(child => {
            if (skipTags.has(child.tagName)) return false;
            if (child.nodeType === Node.TEXT_NODE && !(child.textContent || '').trim()) return false;
            return true;
        }) as HTMLElement[];

        if (children.length < 2) return null;

        // Count tag frequencies
        const tagCounts = new Map<string, number>();
        for (const child of children) {
            tagCounts.set(child.tagName, (tagCounts.get(child.tagName) || 0) + 1);
        }

        // Find dominant tag (>=70% of children)
        let dominantTag = '';
        let dominantCount = 0;
        for (const [tag, count] of tagCounts) {
            if (count > dominantCount) { dominantTag = tag; dominantCount = count; }
        }
        if (dominantCount / children.length < 0.7) return null;

        // Classify by dominant tag and extract values
        let arrayType: 'image_array' | 'text_array' | 'link_array';
        let values: string[] = [];

        switch (dominantTag) {
            case 'IMG':
                arrayType = 'image_array';
                values = children.filter(c => c.tagName === 'IMG')
                    .map(c => (c as HTMLImageElement).currentSrc || (c as HTMLImageElement).src)
                    .filter(Boolean);
                break;
            case 'A':
                arrayType = 'link_array';
                values = children.filter(c => c.tagName === 'A')
                    .map(c => (c as HTMLAnchorElement).href)
                    .filter(v => v && !v.startsWith('javascript:'));
                break;
            case 'VIDEO':
            case 'SOURCE':
                arrayType = 'link_array';
                values = children.filter(c => c.tagName === dominantTag)
                    .map(c => c.getAttribute('src') || '').filter(Boolean);
                break;
            case 'SVG':
            case 'I':
                arrayType = 'text_array';
                values = children.filter(c => c.tagName === dominantTag)
                    .map(c => c.getAttribute('class') || c.textContent?.trim() || '').filter(Boolean);
                break;
            default: {
                // Wrappers containing single images or links
                const dominantChildren = children.filter(c => c.tagName === dominantTag);
                const innerImgs = dominantChildren.filter(c => c.querySelector('img') && !c.querySelector('img ~ img'));
                if (innerImgs.length === dominantCount) {
                    arrayType = 'image_array';
                    values = innerImgs.map(c => {
                        const img = c.querySelector('img') as HTMLImageElement;
                        return img ? (img.currentSrc || img.src) : '';
                    }).filter(Boolean);
                } else {
                    const innerLinks = dominantChildren.filter(c => c.querySelector('a[href]') && !c.querySelector('a[href] ~ a[href]'));
                    if (innerLinks.length === dominantCount) {
                        arrayType = 'link_array';
                        values = innerLinks.map(c => (c.querySelector('a[href]') as HTMLAnchorElement)?.href || '')
                            .filter(v => v && !v.startsWith('javascript:'));
                    } else {
                        arrayType = 'text_array';
                        values = dominantChildren.map(c => (c.textContent || '').trim()).filter(Boolean);
                    }
                }
                break;
            }
        }

        if (values.length < 2) return null;
        return { arrayType, values, count: values.length };
    }
}
