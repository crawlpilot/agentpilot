import type { ISelectionStrategy } from './ISelectionStrategy';
import { VisualElementPicker } from '../VisualElementPicker';
import { ContainerDetector } from '../ContainerDetector';
import { DataExtractor } from '../../../services/extraction/DataExtractor';
import { ItemSelectorRefiner } from '../ItemSelectorRefiner';
import { generateCssSelector, generateXPath, generateRobustSelectors } from '../../../services/dom/domUtils';
import { CommonSelectorGenerator } from '../../../services/dom/selectors/CommonSelectorGenerator';

export class ListSelectionStrategy implements ISelectionStrategy {
    private detector: ContainerDetector;
    private extractor: DataExtractor;
    private refiner: ItemSelectorRefiner;
    private lastContainerInfo: any = null;
    private lastItemUnit: HTMLElement | null = null;
    public preferredAction?: 'extract' | 'click' = 'extract';

    constructor() {
        this.detector = new ContainerDetector();
        this.extractor = new DataExtractor();
        this.refiner = new ItemSelectorRefiner();
    }

    handleHover(element: HTMLElement, picker: VisualElementPicker): void {
        const containerInfo = this.detector.findContainer(element);
        this.applySelection(containerInfo, element, picker);
    }

    private applySelection(containerInfo: any, element: HTMLElement, picker: VisualElementPicker) {
        this.lastContainerInfo = containerInfo;

        if (containerInfo.isContainer && containerInfo.container) {
            // Find the repeating item unit (the card/row)
            const itemUnit = this.refiner.findTargetItem(
                element,
                containerInfo.container,
                containerInfo.siblings
            );
            this.lastItemUnit = itemUnit;

            const typeLabel = containerInfo.containerType === 'table' ? 'Table' : 'List';
            const label = `📋 ${typeLabel} Container (${containerInfo.childCount} items)`;
            const sub = `Click to select entire ${typeLabel.toLowerCase()}`;

            picker.updateHighlightManual(
                containerInfo.container,
                label,
                sub,
                element,
                containerInfo.siblings
            );
        } else {
            this.lastItemUnit = null;
            picker.updateHighlightManual(
                element,
                element.tagName.toLowerCase(),
                'Click to select',
                undefined,
                []
            );
        }
    }

    handleClick(element: HTMLElement, picker: VisualElementPicker, sendResponse: (msg: any) => void): void {
        const containerInfo = this.lastContainerInfo || this.detector.findContainer(element);
        const targetItem = this.lastItemUnit || element;

        // Use the refined item unit for extraction
        const data = this.extractor.extract(targetItem, containerInfo);

        let selector = '';
        let xpath = '';
        let containerSelector = '';
        let containerXPath = '';
        let containerMetadata: any[] = [];
        let itemMetadata: any[] = [];

        if (containerInfo.isContainer && containerInfo.container) {
            containerSelector = generateCssSelector(containerInfo.container);
            containerXPath = generateXPath(containerInfo.container);
            containerMetadata = generateRobustSelectors(containerInfo.container);

            // DEDUCTIVE APPROACH: Strictly use selectors that match the visual capture
            // Our CommonSelectorGenerator is now powerful enough to derive these from the siblings.
            const commonGen = new CommonSelectorGenerator();
            const validResults = commonGen.deriveCommonSelector(containerInfo.siblings, containerInfo.container, targetItem);

            let candidates: any[] = [];
            if (validResults.length > 0) {
                selector = validResults[0].selector;
                candidates = validResults.map(res => ({
                    selector: res.selector,
                    strategy: `Deductive ${res.type}`
                }));
            } else {
                // If even the deductive generator fails, default to a basic tag or structural selector
                // to avoid empty candidates, but avoid the "guessing" generator here as it's often wrong.
                selector = generateCssSelector(targetItem, true);
                candidates = [{ selector: selector, strategy: 'Deductive Fallback' }];
            }


            // [FIX] Re-query container for ALL items using derived selector
            // This ensures preview extraction processes the SAME items as final extraction
            // (final uses querySelectorAll, preview was using filtered siblings from ContainerDetector)
            if (selector && containerInfo.container) {
                try {
                    const allMatches = containerInfo.container.querySelectorAll(selector);
                    const allItems = Array.from(allMatches) as HTMLElement[];

                    if (allItems.length > 0) {
                        containerInfo.siblings = allItems;
                        containerInfo.childCount = allItems.length;
                    }
                } catch (e) {
                    // Silently use original filtered siblings if selector fails
                }
            }

            xpath = generateXPath(targetItem);

            // Prioritize the best selector in the robust list for the UI
            // This ensures the suggested "attribute extraction selector" matches the visual one
            itemMetadata = candidates;

            sendResponse({
                type: 'ELEMENT_SELECTED',
                payload: {
                    success: true,
                    id: `sel-${Date.now()}`,
                    data: data,
                    containerSelector: containerSelector,
                    containerSelectors: containerMetadata,
                    containerXPath: containerXPath,
                    itemSelector: selector,
                    itemSelectors: itemMetadata,
                    itemXPath: xpath,
                    patternFound: true,
                    count: containerInfo.childCount,
                    selectionMode: 'list'
                }
            });
        } else {
            selector = generateCssSelector(element, false);
            xpath = generateXPath(element);
            containerSelector = selector;
            containerXPath = xpath;
            const elementResults = generateRobustSelectors(element);
            containerMetadata = elementResults;
            itemMetadata = elementResults;

            sendResponse({
                type: 'ELEMENT_SELECTED',
                payload: {
                    success: true,
                    id: `sel-${Date.now()}`,
                    data: data,
                    containerSelector: containerSelector,
                    containerSelectors: containerMetadata,
                    containerXPath: containerXPath,
                    itemSelector: selector,
                    itemSelectors: itemMetadata,
                    itemXPath: xpath,
                    patternFound: false,
                    count: 1,
                    selectionMode: 'list'
                }
            });
        }

        // Click logic removed from selection phase to prevent unintended navigation

        picker.deactivate();
    }
}
