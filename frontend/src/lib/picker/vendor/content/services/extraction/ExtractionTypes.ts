export type ColumnType = 'text' | 'image' | 'url' | 'price' | 'email' | 'number' | 'date' | 'phone' | 'image_array' | 'text_array' | 'link_array';

export interface Column {
    id: string;
    originalId: string;
    name: string;
    type: ColumnType;
    order?: number;
    selector?: string;
    isVirtual?: boolean;
    mergedKeys?: string[];
    /** Raw keys that alias this column across row variants; coalesced at normalize time */
    sourceKeys?: string[];
    validation?: {
        required?: boolean;
        pattern?: string;
    };
    imageMetadata?: {
        maxWidth?: number;
        maxHeight?: number;
        maxArea?: number;
    };
}

export interface ExtractionResult {
    type: 'list' | 'single';
    columns: Column[];
    items: any[];
    source_url: string;
    count: number;
    timestamp: string;
    metadata?: any;
}

export interface ProgressUpdate {
    type: 'progress' | 'data' | 'complete' | 'error';
    status?: string;
    message?: string;
    page?: number;
    count?: number;
    data?: any;
    error?: string;
}

export interface SelectorConfig {
    selector: string;
    type: string;
}

export interface ExtractionConfig {
    selector?: string;
    container?: HTMLElement;
    elements?: HTMLElement[];
    selectionMode: 'single' | 'list';
    columns?: Column[]; // Preview schema for enforcement
}
