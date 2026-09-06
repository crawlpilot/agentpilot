/**
 * The contract between the two halves of the picker.
 *
 * One half (`entry.ts` + `vendor/**`) is bundled into an IIFE and runs inside
 * the remote page. The other (`hooks/usePagePicker.ts`, the wizard) runs in
 * the studio. They never share a module instance -- one is a string injected
 * over CDP -- so the only thing that can keep them honest is a type-only
 * contract both compile against. That is this file.
 *
 * It must stay free of runtime imports: `entry.ts` pulls it into the injected
 * bundle, and the studio pulls it into the app bundle.
 */

/** Bumped when the injected contract changes, so a stale page re-installs. */
export const PICKER_VERSION = 1

/** The global the IIFE installs itself on inside the remote page. */
export const PICKER_GLOBAL = '__cpPicker'

/**
 * `list`   -- pick one item of a repeating set; yields container + item selectors.
 * `detail` -- pick a single field; yields a classified type and a preview value.
 * `single` -- pick one control (the next/load-more button); no extraction.
 */
export type PickerMode = 'list' | 'detail' | 'single'

/** What the picker parks on `window.__cpPickResult`, collected by `take()`. */
export type PickMessage =
  | { type: 'ELEMENT_SELECTED'; payload: PickPayload }
  | { type: 'PICKER_CANCELLED'; payload?: undefined }

/** One entry of a ranked selector chain, as the vendored generators emit it. */
export interface PickSelector {
  selector: string
  /** Which generator produced it -- "Minimal", "Deductive CSS", "Semantic XPath". */
  strategy: string
}

/**
 * The union of what the three strategies emit. Fields present in only one
 * mode are optional; `selectionMode` says which shape actually arrived.
 *
 * This mirrors `ListSelectionStrategy.handleClick` /
 * `DetailSelectionStrategy.handleClick` / `SingleSelectionStrategy.handleClick`
 * upstream. `src/lib/recipe/fromPick.ts` is the only consumer.
 */
export interface PickPayload {
  success: boolean
  id: string
  selectionMode: PickerMode

  /** The repeating container, and the item within it. */
  containerSelector: string
  containerSelectors?: PickSelector[]
  containerXPath?: string
  itemSelector?: string
  itemSelectors?: PickSelector[]
  itemXPath?: string

  /** True when a repeating pattern was actually found (list mode). */
  patternFound: boolean
  /** How many elements the derived item selector matched. */
  count: number

  /** Extracted rows + inferred columns (`DataExtractor` / `SchemaGenerator`). */
  data?: PickData

  // --- detail mode only ---
  /** The auto-classified kind, from `DetailSelectionStrategy.classify`. */
  extractionType?: PickExtractionType
  /** A short human-readable sample of what this element yields. */
  previewValue?: string
  value?: string
  tagName?: string
  action?: 'extract' | 'click'
}

export type PickExtractionType =
  | 'text'
  | 'link'
  | 'image'
  | 'list'
  | 'table'
  | 'text_array'
  | 'link_array'
  | 'image_array'

/** `ColumnType` as `SchemaGenerator` infers it. */
export type PickColumnType =
  | 'text'
  | 'number'
  | 'url'
  | 'image'
  | 'image_array'
  | 'email'
  | 'phone'
  | 'date'
  | 'price'
  | 'markdown'

export interface PickColumn {
  id: string
  name: string
  type: PickColumnType
  /**
   * The extractor's own path key (e.g. `"prod > h3"`) -- human-readable, and
   * NOT a CSS selector. Kept because `SchemaGenerator` keys off it; never use
   * it as a locator. `locators` is the addressable form.
   */
  selector?: string
  order?: number

  // --- added by `enrich.ts`, in the page, at pick time ---
  /** Ranked selectors for this column's value, relative to its row. */
  locators?: PickSelector[]
  /** Row-relative XPath fallback for the same element. */
  xpath?: string
  /** `href` / `src` when the value is an attribute rather than text. */
  attribute?: string
}

export interface PickData {
  type?: string
  columns: PickColumn[]
  items: Record<string, unknown>[]
  count?: number
  source_url?: string
}

/** The surface `entry.ts` installs on `window.__cpPicker`. */
export interface PickerApi {
  version: number
  start(mode?: PickerMode, action?: 'extract' | 'click'): void
  cancel(): void
  action(key: 'ArrowUp' | 'ArrowDown' | 'Enter'): void
  take(): PickMessage | null
  isPicking(): boolean
  showHighlights(elements: unknown[]): void
  clearHighlights(): void
  testSelector(selector: string): number
  /**
   * Resolve fields against the live page, mirroring the replay engine.
   * See `preview.ts` -- values are pre-transform.
   */
  preview(fields: unknown[]): unknown[]
}
