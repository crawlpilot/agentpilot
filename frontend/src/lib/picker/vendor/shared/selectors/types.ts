export type { SelectorMetadata, SelectorConfig, SelectorState } from '../types/extraction';

/** A single generic selector entry, normalized from any of the shapes used across the codebase:
 *  - SelectorMetadata { selector, strategy }   (picker output)
 *  - SelectorDefinition { type: 'css'|'xpath'|'id', value }  (persisted recipes)
 *  - a plain string
 */
export interface ChainEntry {
    value: string;
    kind: 'css' | 'xpath';
    strategy?: string;
}

/** Any shape resolveFirst/resolveAll accept as an individual chain entry before normalization. */
export type RawChainEntry =
    | string
    | { selector: string; strategy?: string }
    | { value: string; type?: string };
