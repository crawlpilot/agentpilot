/**
 * VENDORED from crawlPilot @ a9355a8 (release-2.0.0),
 * `src/shared/types/extraction.ts`.
 *
 * Trimmed to the picker's own surface. Upstream also carries the extension's
 * runtime state (`ExtractionStatus`, orchestrator progress) and imports
 * `PaginationSpeed` from `shared/constants/paginationConfig`; neither is part
 * of the picker, so `PaginationSpeed` is inlined here and the runtime state is
 * dropped rather than dragging the extension's job machinery across.
 *
 * These are the shapes the picker *emits*. `src/lib/recipe/fromPick.ts` reads
 * them and nothing else, so this file is the contract between the vendored
 * core and the studio.
 */

export interface SelectorMetadata {
    /** The selector text itself -- CSS or XPath, disambiguated by `strategy`. */
    selector: string;
    /** Which generator produced it, e.g. "Minimal", "Deductive CSS", "Semantic XPath". */
    strategy: string;
}

export interface SelectorConfig {
    id: string;
    containerSelector: string; // Primary
    containerSelectors?: SelectorMetadata[];
    containerXPath?: string;
    itemSelector?: string;
    itemSelectors?: SelectorMetadata[];
    itemXPath?: string;
}

export interface SelectorState extends SelectorConfig {
    data: any;
    patternFound: boolean;
    count: number;
    success?: boolean;
    selectionMode?: 'list' | 'single' | 'detail';
}

export type PaginationSpeed = 'slow' | 'normal' | 'fast';

export type PaginationMode = 'auto' | 'pagination' | 'loadMore';

export interface PaginationConfig {
    mode: PaginationMode;
    nextButtonSelector: string | null;
    nextButtonSelectors?: SelectorMetadata[];
    speed?: PaginationSpeed;
    maxPages?: number;
}
