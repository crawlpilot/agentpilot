// TypeScript mirror of `docs/schemas/recipe-v2.schema.json`.
//
// The schema is the normative definition; this file exists so the studio can
// edit a recipe with the compiler checking the shape, and so a mistake shows
// up while typing rather than as a 422 from `/validate`. Property names,
// enum members and defaults are kept identical to the schema on purpose --
// the JSON tab round-trips a document through these types, so any divergence
// would silently rewrite a field the studio does not understand.
//
// The schema allows `_`-prefixed annotation keys anywhere (`patternProperties`).
// Those are authoring notes, not contract; `RecipeAnnotations` carries them
// through the editor untouched so an agent-written `_why` survives a save.

export type UrlMatcherKind = 'glob' | 'regex' | 'host'

export interface UrlMatcher {
  kind: UrlMatcherKind
  pattern: string
}

export interface TargetSpec {
  match: UrlMatcher[]
}

export type LocatorKind = 'css' | 'xpath' | 'ax_role' | 'text' | 'json_ld' | 'hydration' | 'meta'

/** The three kinds that read a path out of page-embedded JSON rather than the DOM. */
export const STRUCTURED_KINDS: LocatorKind[] = ['json_ld', 'hydration', 'meta']

export type PathLang = 'simple' | 'jmespath'

export interface Locator {
  kind: LocatorKind
  // css / xpath
  selector?: string
  index?: number | null
  all?: boolean
  attribute?: string
  // ax_role
  role?: string
  name_contains?: string | null
  name_in?: string[] | null
  name_regex?: string | null
  // json_ld / hydration / meta
  path?: string
  path_lang?: PathLang
  // text
  text?: string
  // scoping
  within?: Locator
  frame?: string | null
}

export type PredicateKind =
  | 'selector_present'
  | 'selector_absent'
  | 'visible'
  | 'text_present'
  | 'url_matches'
  | 'json_path_present'
  | 'count_at_least'
  | 'meta_equals'

export interface Predicate {
  kind: PredicateKind
  selector?: string
  text?: string
  url?: string
  path?: string
  source?: 'json_ld' | 'hydration' | 'meta'
  key?: string
  value?: string
  n?: number
}

export type StepOp =
  | 'navigate'
  | 'click'
  | 'double_click'
  | 'hover'
  | 'fill'
  | 'clear'
  | 'press'
  | 'send_keys'
  | 'select_option'
  | 'check'
  | 'uncheck'
  | 'scroll'
  | 'scroll_into_view'
  | 'find_text'
  | 'drag'
  | 'tap'
  | 'swipe'
  | 'wait'
  | 'wait_for_selector'
  | 'wait_for_text'
  | 'wait_for_url'
  | 'wait_for_load'
  | 'dialog_accept'
  | 'dialog_dismiss'
  | 'new_tab'
  | 'switch_tab'
  | 'close_tab'
  | 'download'

export type OnError = 'fail' | 'continue' | 'skip_group'

export interface RetrySpec {
  attempts: number
  backoff_ms?: number
}

export interface Step {
  op: StepOp
  target?: Locator
  args?: Record<string, unknown>
  timeout_ms?: number | null
  retry?: RetrySpec | null
  on_error?: OnError
  optional?: boolean
  when?: Predicate[]
  repeat_until?: Predicate
  max_repeats?: number
  label?: string | null
}

export interface PageVariant {
  variant_id: string
  priority?: number
  detect: Predicate[]
  label?: string | null
}

export type ValueType =
  | 'string'
  | 'text'
  | 'number'
  | 'float'
  | 'integer'
  | 'price'
  | 'boolean'
  | 'url'
  | 'date'
  | 'datetime'
  | 'json'

export type TypeKind = 'scalar' | 'list' | 'object' | 'table'

export interface TypeSpec {
  kind: TypeKind
  value_type?: ValueType
  items?: TypeSpec
  properties?: Record<string, TypeSpec>
  columns?: Record<string, TypeSpec>
}

export type TransformOp =
  | 'regex_extract'
  | 'regex_replace'
  | 'trim'
  | 'collapse_ws'
  | 'strip_control'
  | 'strip_accents'
  | 'case'
  | 'split'
  | 'join'
  | 'slice'
  | 'index'
  | 'unique'
  | 'filter_empty'
  | 'map_lookup'
  | 'template'
  | 'url_resolve'
  | 'json_parse'
  | 'json_path'
  | 'strip_html'
  | 'html_select'
  | 'to_object'
  | 'to_pairs'
  | 'cast'
  | 'default'
  | 'lua'

export interface Transform {
  op: TransformOp
  // html_select
  selector?: string
  attribute?: string
  all?: boolean
  // to_object / json_path
  key?: string
  path?: string
  path_lang?: PathLang
  // regex_*
  pattern?: string
  group?: number
  flags?: string
  repl?: string
  count?: number
  // case
  mode?: 'lower' | 'upper' | 'title'
  // split / join
  sep?: string
  limit?: number
  // slice / index
  start?: number | null
  end?: number | null
  i?: number
  // map_lookup
  table?: Record<string, unknown>
  default?: unknown
  // template
  format?: string
  // cast
  to?: ValueType
  // default
  value?: unknown
  // url_resolve
  source?: string
}

export type AssertionKind =
  | 'range'
  | 'matches'
  | 'in_set'
  | 'length'
  | 'not_empty'
  | 'cross_source_agrees'
  | 'not_equals_previous'

export interface Assertion {
  kind: AssertionKind
  min?: number | null
  max?: number | null
  regex?: string
  values?: unknown[]
  tolerance?: number
}

export interface FieldSpec {
  description?: string
  type: TypeSpec
  required?: boolean
  emit_raw?: boolean
  transform?: Transform[]
  assertions?: Assertion[]
}

export interface Candidate {
  priority?: number
  locator: Locator
  when?: Predicate[]
  variant_id?: string | null
  transform?: Transform[] | null
  verified_on?: number
  confidence?: number | null
  note?: string | null
}

export interface RepeatSpec {
  kind: 'dom' | 'json'
  option_locator?: Locator
  action?: StepOp
  settle?: Step
  rows_locator?: Locator
  max_iterations: number
  row_field: string
}

export interface GroupExpect {
  min_rows?: number | null
  max_rows?: number | null
}

export interface FieldGroup {
  group_id: string
  field_names: string[]
  steps?: Step[]
  bindings: Record<string, Candidate[]>
  repeat?: RepeatSpec
  expect?: GroupExpect
}

export interface ExecutionDefaults {
  step_timeout_ms?: number
  navigate_timeout_ms?: number
  max_repeat_iterations?: number
  lua_timeout_ms?: number
}

export type RecipeStatus = 'draft' | 'approved' | 'published'

export interface BuiltUnder {
  tier?: string
  proxy_region?: string | null
}

export interface Recipe {
  recipe_id?: string
  tenant?: string
  name: string
  version?: number
  status?: RecipeStatus
  target: TargetSpec
  fields: Record<string, FieldSpec>
  variants?: PageVariant[]
  global_setup?: Step[]
  field_groups: FieldGroup[]
  defaults?: ExecutionDefaults
  sample_urls: string[]
  built_under?: BuiltUnder
  has_script?: boolean
  health_status?: string
  heal_attempts?: number
  schedule_interval_seconds?: number | null
  notes?: string
}

// --- §9 result model, rendered by the preview bar ---

export type FieldStatus = 'resolved' | 'fallback' | 'suspect' | 'empty' | 'failed'
export type RunOutcome = 'ok' | 'partial' | 'failed' | 'blocked'

export interface FieldProvenance {
  candidate: number
  source: LocatorKind
  variant: string | null
}

export interface AssertionResult {
  kind: AssertionKind
  passed: boolean
  detail?: string | null
}

export interface StepOutcome {
  group_id: string | null
  op: StepOp
  label?: string | null
  status: 'ok' | 'skipped' | 'recovered' | 'failed'
  duration_ms: number
  reason?: string | null
}

export interface RecipeRunResult {
  outcome: RunOutcome
  data: Record<string, unknown>
  field_status: Record<string, FieldStatus>
  provenance: Record<string, FieldProvenance>
  truncated: Record<string, boolean>
  assertions: Record<string, AssertionResult[]>
  step_trace: StepOutcome[]
  error: string | null
}
