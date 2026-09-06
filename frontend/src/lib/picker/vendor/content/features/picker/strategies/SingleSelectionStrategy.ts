import type { ISelectionStrategy } from './ISelectionStrategy';
import { VisualElementPicker } from '../VisualElementPicker';
import { DataExtractor } from '../../../services/extraction/DataExtractor';
import { generateCssSelector, generateXPath } from '../../../services/dom/domUtils';
import { PaginationSelectorGenerator } from '../../../services/dom/selectors/PaginationSelectorGenerator';

export class SingleSelectionStrategy implements ISelectionStrategy {
    private extractor: DataExtractor;

    public preferredAction?: 'extract' | 'click' = 'extract';

    constructor() {
        this.extractor = new DataExtractor();
    }

    handleHover(element: HTMLElement, picker: VisualElementPicker): void {
        picker.updateHighlightManual(
            element,
            'Next Button',
            'Click to select',
            undefined,
            []
        );
    }

    handleClick(element: HTMLElement, picker: VisualElementPicker, sendResponse: (msg: any) => void): void {
        console.log('Single Strategy Selected:', element);

        const data = this.extractor.extract(element, { isContainer: false, siblings: [], container: null });
        const selector = generateCssSelector(element, false);
        const xpath = generateXPath(element);

        // [NEW] Use the specialized PaginationSelectorGenerator
        const generator = new PaginationSelectorGenerator();
        const paginationSelectors = generator.generateForPagination(element);

        // Pick the best selector for immediate display (priority 0 is highest)
        const bestSelector = paginationSelectors.length > 0 ? paginationSelectors[0].selector : selector;
        const bestSelectorType = paginationSelectors.length > 0 && paginationSelectors[0].selector.startsWith('//') ? 'xpath' : 'selector';

        // If the best strategy is semantic (likely XPath), use it as the primary 'itemSelector'
        // This ensures the UI input box shows the semantic one by default
        const primarySelector = bestSelector;
        const primaryXPath = bestSelectorType === 'xpath' ? bestSelector : xpath;

        sendResponse({
            type: 'ELEMENT_SELECTED',
            payload: {
                success: true,
                id: `sel-${Date.now()}`,
                data: data,
                containerSelector: primarySelector,
                containerSelectors: paginationSelectors,
                containerXPath: primaryXPath,
                itemSelector: primarySelector,
                itemSelectors: paginationSelectors,
                itemXPath: primaryXPath,
                patternFound: false,
                count: 1,
                selectionMode: 'single'
            }
        });

        picker.deactivate();

        // Click logic removed from selection phase to prevent unintended navigation

    }
}
