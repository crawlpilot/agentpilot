/**
 * Extraction Strategy Interface
 * Defines common contract for all extraction strategies
 */

export interface ExtractionConfig {
    containerRoot: HTMLElement;
    enableSemanticFields?: boolean;
    enableCSSSelectors?: boolean;
    enableDeduplication?: boolean;
    confidenceThreshold?: number;
}

export interface RowData {
    data: Record<string, any>;
    metadata: {
        selector?: string;
        confidence?: number;
        timestamp: string;
    };
}

export interface IExtractionStrategy {
    /**
     * Extract structured data from an HTML element
     * @param element - The HTML element to extract data from
     * @param config - Extraction configuration
     * @returns Extracted row data with metadata
     */
    extract(element: HTMLElement, config: ExtractionConfig): RowData;

    /**
     * Get strategy name for logging/debugging
     */
    getName(): string;
}
