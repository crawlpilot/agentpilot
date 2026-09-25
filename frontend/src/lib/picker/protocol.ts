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
export const PICKER_VERSION = 4

/** The global the IIFE installs itself on inside the remote page. */
export const PICKER_GLOBAL = '__cpPicker'

/**
 * `list`   -- pick one item of a repeating set; yields container + item selectors.
 * `detail` -- pick a single field; yields a classified type and a preview value.
 * `single` -- pick one control (the next/load-more button); no extraction.
 */
export type PickerMode = 'list' | 'detail' | 'single'

/**
 * What one entry of a recorded route is *for*.
 *
 * The extension had half of this already: `ElementDefinition.action` tags each
 * entry of one ordered list as `'extract'` or `'click'`, and
 * `ElementProcessor.performAction` switches on it. A recording adds ordering
 * and two more cases, because a route is not only a set of elements -- some of
 * its clicks are load-bearing and some are housekeeping, and replay must treat
 * them differently:
 *
 * - `reveal`     -- opens the accordion, submits the search. The field is not
 *                   there without it, so a miss must be attributable.
 * - `dismiss`    -- a cookie banner, a modal's ×. May legitimately be absent on
 *                   a given run, so it replays `optional`.
 * - `settle`     -- a wait for what a reveal revealed. See `Recorder.probeReveal`.
 * - `select`     -- the pick. Carries the payload the binding is read from, and
 *                   is the only intent that is not replayed as a step.
 * - `incidental` -- scrolling and strays. Kept so the list reads like what the
 *                   person did; safe to drop.
 *
 * Absent means `reveal`, which is what every step recorded before intents
 * existed was in practice.
 */
export type StepIntent = 'reveal' | 'dismiss' | 'settle' | 'select' | 'incidental'

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

  /**
   * Added by `enrich.ts`: the rows read as *label -> value*, not as records.
   *
   * Only set when the markup says so -- a `th` or `dt` leading every row. See
   * `enrich.ts::looksKeyValue`; the wizard offers it as a toggle for the many
   * spec tables built from plain `td`s.
   */
  keyValue?: boolean

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

/**
 * One persistent on-page marker for a field the author has already picked.
 *
 * Mirrors the extension's `HighlightableElement`. `action` drives the colour:
 * green for something being read, amber for something being clicked.
 */
export interface HighlightField {
  id: string
  name: string
  action: 'extract' | 'click'
  selectors: { type: string; value: string }[]
}

/** The surface `entry.ts` installs on `window.__cpPicker`. */
export interface PickerApi {
  version: number
  start(mode?: PickerMode, action?: 'extract' | 'click'): void
  cancel(): void
  /**
   * `ArrowUp`/`ArrowDown` walk the tree and PIN the selection so a passing
   * cursor cannot take it back; `Unpin` hands it back to hover; `Enter`
   * commits. See `VisualElementPicker.pinned` for why pinning is needed here
   * and was not upstream.
   */
  action(key: 'ArrowUp' | 'ArrowDown' | 'Enter' | 'Unpin'): void
  /** What the selection is on right now, so the panel can describe it. */
  selection(): { tag: string; text: string; pinned: boolean } | null
  take(): PickMessage | null
  isPicking(): boolean
  /**
   * Watch what the person does to the page and hand it back as reveal steps.
   *
   * Not a picker: nothing is swallowed and no overlay is drawn, because the
   * page has to actually react or there is nothing to record. See `record.ts`.
   */
  startRecording(): void
  stopRecording(): unknown[]
  takeRecording(): unknown[]
  isRecording(): boolean
  /**
   * Pick an element *during* a recording, and fold it into the route in order.
   *
   * The second interaction, and the one the panel had no way to express: a
   * route could say how to get to a field or which element it was, never both
   * in sequence. Pauses the recorder (the picker swallows the clicks it draws
   * over, and those are not the person's), runs an ordinary `detail` pick, and
   * pushes the result as a `select` entry before resuming.
   *
   * `action: 'click'` is the extension's other half -- the picked element
   * becomes a reveal step rather than a binding. Reachable only from here;
   * `pick()` has always defaulted `detail` to `'extract'`.
   */
  pickInRecording(action?: 'extract' | 'click'): void
  /** Open but not observing -- a pick is in flight. */
  isRecordingPaused(): boolean
  /**
   * Whether the page changed under the recording.
   *
   * Every step after a navigation targets a different document, and because
   * reveal steps replay `on_error: continue` the route then fails in total
   * silence. `Recorder` has tracked this since it was written; until now
   * nothing could ask.
   */
  didNavigate(): boolean
  /** Whether the step cap has been hit and events are being discarded. */
  recordingFull(): boolean
  showHighlights(elements: HighlightField[]): void
  clearHighlights(): void
  testSelector(selector: string): number
  /**
   * Resolve fields against the live page, mirroring the replay engine.
   * See `preview.ts` -- values are pre-transform.
   */
  preview(fields: unknown[]): unknown[]
  /** Row-wise read for a `dom_rows` table. See `preview.ts`. */
  previewRows(fields: unknown[]): unknown[]
  /**
   * Apply reveal steps in the page before a preview. A rehearsal, not replay
   * -- see `preview.ts::APPLY_STEPS_JS`.
   */
  applySteps(steps: unknown[]): Promise<unknown[]>
}
