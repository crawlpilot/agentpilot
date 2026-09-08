// Hand-mirrored from `agentpilot/gateway/schemas.py` -- kept in sync by hand for
// now (the schema surface is small and still moving; revisit codegen once it
// stabilizes, see the frontend architecture notes in the enterprise-UI plan).

export type Tier = 'basic' | 'stealth' | 'enhanced' | 'auto'

export interface SessionOpenRequest {
  tenant: string
  domain: string
  name: string
  tier?: Tier
  headful?: boolean
  block_popups?: boolean
  live_view?: boolean
  enable_cdp?: boolean
}

export interface SessionMetadata {
  tier_used: string
  node_id: string
  duration_ms: number
}

export interface SessionOpenResponse {
  session_id: string
  metadata: SessionMetadata
}

export interface SessionOut {
  session_id: string
  tenant: string
  domain: string
  name: string
  tier: string
  headful: boolean
  enable_cdp: boolean
  node_id: string
  pid: number | null
  rss_mb: number | null
  state: 'active' | 'expired'
  lease_expires_at: number | null
}

export interface SessionListOut {
  sessions: SessionOut[]
}

// --- CDP discovery (routes/cdp.py's `GET .../cdp/json/version` -- Chrome's
// own `/json/version` shape verbatim, field names included, so any
// off-the-shelf CDP client that already knows how to read a real browser's
// discovery doc reads this one identically) ---

export interface CdpDiscoveryOut {
  Browser: string
  'Protocol-Version': string
  'User-Agent': string
  'V8-Version': string
  'WebKit-Version': string
  webSocketDebuggerUrl: string
}

// --- fleet (nodes dashboard) ---

export interface NodeOut {
  node_id: string
  addr: string | null
  started_at: number | null
  live: boolean
  max_contexts: number | null
  active: number | null
  idle: number | null
  mem_used_pct: number | null
  cpu_used_pct: number | null
}

export interface NodeListOut {
  nodes: NodeOut[]
}

// --- actions (discriminated union mirroring schemas.py's ActionIn) ---

export interface NavigateAction {
  type: 'navigate'
  url: string
  timeout_ms?: number
}
export interface GoBackAction {
  type: 'go_back'
}
export interface SnapshotAction {
  type: 'snapshot'
  viewport_only?: boolean
  max_nodes?: number | null
  roles?: string[] | null
  with_bbox?: boolean
}
export interface ExtractAction {
  type: 'extract'
  format?: ScrapeFormat
  main_content?: boolean
  include_tags?: string[]
  exclude_tags?: string[]
}
export interface ScreenshotAction {
  type: 'screenshot'
  full_page?: boolean
}
export interface WaitAction {
  type: 'wait'
  ms?: number | null
  ref?: string | null
}
export interface ExecuteJsAction {
  type: 'execute_js'
  script: string
}
export interface ClickAction {
  type: 'click'
  ref: string
  all?: boolean
}
export interface FillAction {
  type: 'fill'
  ref: string
  text: string
}
export interface SelectOptionAction {
  type: 'select_option'
  ref: string
  values: string[]
}
export interface HoverAction {
  type: 'hover'
  ref: string
}
export interface PressAction {
  type: 'press'
  key: string
}
export interface ScrollAction {
  type: 'scroll'
  direction: 'up' | 'down' | 'left' | 'right'
  ref?: string | null
}

// --- tab management (mirrors agentpilot/spi/actions.py's NewTab/CloseTab/SwitchTab/ListTab) ---

export interface NewTabAction {
  type: 'new_tab'
  url?: string | null
}
export interface CloseTabAction {
  type: 'close_tab'
  page_id: string
}
export interface SwitchTabAction {
  type: 'switch_tab'
  page_id: string
}
export interface ListTabsAction {
  type: 'list_tabs'
}

export type ActionIn =
  | NavigateAction
  | GoBackAction
  | SnapshotAction
  | ExtractAction
  | ScreenshotAction
  | WaitAction
  | ExecuteJsAction
  | ClickAction
  | FillAction
  | SelectOptionAction
  | HoverAction
  | PressAction
  | ScrollAction
  | NewTabAction
  | CloseTabAction
  | SwitchTabAction
  | ListTabsAction

export const ACTION_TYPES = [
  'navigate',
  'go_back',
  'snapshot',
  'extract',
  'screenshot',
  'wait',
  'execute_js',
  'click',
  'fill',
  'select_option',
  'hover',
  'press',
  'scroll',
  'new_tab',
  'close_tab',
  'switch_tab',
  'list_tabs',
] as const

export interface ExecuteRequest {
  actions: ActionIn[]
  page_id?: string | null
}

export interface SnapshotNode {
  epoch: number
  ref: string
  role: string
  name: string
  children: SnapshotNode[]
}

export interface AXSnapshot {
  epoch: number
  root: SnapshotNode
}

export interface ArtifactRef {
  artifact_id: string
  tenant: string
  kind: string
  size: number
  sha256: string
}

export interface TabInfo {
  page_id: string
  url: string
  title: string
  active: boolean
}

export interface ActionResult {
  snapshots: AXSnapshot[]
  screenshots: string[] // base64 PNG
  extracts: string[]
  js_returns: unknown[]
  downloads: ArtifactRef[]
  tabs: TabInfo[][] // one entry per `list_tabs` action in the batch
  sequence_aborted: boolean
  page_changed: boolean
}

// --- scrape (routes/scrape.py) ---

export type ScrapeFormat = 'markdown' | 'text' | 'html' | 'structured_data'

export interface ExtractConfigIn {
  json_schema?: Record<string, unknown> | null
  prompt?: string | null
}

export interface ScrapeRequest {
  tenant: string
  url: string
  tier?: Tier
  formats?: ScrapeFormat[]
  only_main_content?: boolean
  include_tags?: string[]
  exclude_tags?: string[]
  timeout_ms?: number
  wait_for_ms?: number | null
  actions?: ActionIn[]
  screenshot?: boolean
  full_page_screenshot?: boolean
  extract?: ExtractConfigIn | null
  session_name?: string | null
  locale?: string | null
  timezone_id?: string | null
}

export interface ScrapeMetadataOut {
  title: string | null
  status_code: number | null
  tier_used: string
  node_id: string
  duration_ms: number
  source_url: string
}

export interface DocumentOut {
  document_id: string
  url: string
  markdown?: string | null
  text?: string | null
  html?: string | null
  structured_data?: Record<string, unknown> | null
  links: string[]
  screenshot?: string | null // base64 PNG
  metadata?: ScrapeMetadataOut | null
  error?: string | null
  extract?: Record<string, unknown> | unknown[] | null
  /** An array when the request's `extract.json_schema` had an array at its root. */
  extract_error?: string | null
  /** Non-fatal degradation of an extraction that still produced a result
   *  (today: page content truncated to fit the model's input budget). */
  extract_warning?: string | null
}

export interface ScrapeResponse {
  success: boolean
  data: DocumentOut
}

// --- map (routes/map.py) ---

export type SitemapMode = 'skip' | 'include' | 'only'

export interface MapRequest {
  tenant: string
  url: string
  include_paths?: string[]
  exclude_paths?: string[]
  sitemap?: SitemapMode
  include_subdomains?: boolean
  ignore_query_parameters?: boolean
  limit?: number
  search?: string
  allow_external_links?: boolean
  filter_by_path?: boolean
  max_discovery_depth?: number
  timeout?: number
}

export interface MapLinkOut {
  url: string
  title?: string | null
  description?: string | null
}

export interface MapResponse {
  success: boolean
  links: MapLinkOut[]
  warning?: string | null
}

// --- crawl (routes/crawl.py, async job -- POST creates, GET polls, DELETE cancels) ---

export interface ScrapeOptionsIn {
  formats?: ScrapeFormat[]
  only_main_content?: boolean
  include_tags?: string[]
  exclude_tags?: string[]
  timeout_ms?: number
  wait_for_ms?: number | null
  screenshot?: boolean
  full_page_screenshot?: boolean
  extract?: ExtractConfigIn | null
}

export type WebhookEvent = 'started' | 'page' | 'completed' | 'failed'

export interface WebhookIn {
  url: string
  headers?: Record<string, string>
  events?: WebhookEvent[]
}

export interface CrawlRequest {
  tenant: string
  url: string
  include_paths?: string[]
  exclude_paths?: string[]
  max_discovery_depth?: number | null
  limit?: number
  allow_external_links?: boolean
  allow_subdomains?: boolean
  allow_backward_crawling?: boolean
  ignore_robots_txt?: boolean
  sitemap?: SitemapMode
  deduplicate_similar_urls?: boolean
  ignore_query_parameters?: boolean
  delay_ms?: number | null
  max_concurrency?: number
  scrape_options?: ScrapeOptionsIn
  webhook?: WebhookIn | null
}

export interface CrawlCreateResponse {
  success: boolean
  id: string
  url: string
  webhook_secret?: string | null
}

export type CrawlJobStatus = 'queued' | 'scraping' | 'completed' | 'failed' | 'cancelled'

export interface CrawlStatusResponse {
  success: boolean
  status: CrawlJobStatus
  total: number
  completed: number
  failed: number
  data: DocumentOut[]
  next: string | null
}

// --- API keys ---

export interface ApiKeyCreateRequest {
  tenant: string
  name: string
}

export interface ApiKeyOut {
  key_id: string
  tenant: string
  name: string
  prefix: string
  created_at: string
  last_used_at: string | null
  revoked_at: string | null
}

export interface ApiKeyCreateOut extends ApiKeyOut {
  api_key: string
}

export interface ApiKeyListOut {
  api_keys: ApiKeyOut[]
}

// --- errors (agentpilot/gateway/errors.py's closed ErrorCode enum) ---

export type ErrorCode =
  | 'BAD_REQUEST'
  | 'NOT_FOUND'
  | 'SESSION_LEASE_CONFLICT'
  | 'CAPACITY_EXHAUSTED'
  | 'NODE_LOST'
  | 'NAVIGATION_TIMEOUT'
  | 'CHALLENGE_UNRESOLVED'
  | 'CONTEXT_CRASHED'
  | 'EGRESS_BLOCKED'
  | 'STALE_REF'
  | 'CDP_NOT_AVAILABLE'
  | 'JOB_NOT_FOUND'
  | 'JOB_CANCELLED'
  | 'INTERNAL_ERROR'

export interface ApiErrorBody {
  success: false
  code: ErrorCode
  error: string
  details?: unknown
}

// Shared by AgentRunOut and RecipeRunOut -- both use this exact literal
// union (unlike CrawlJobStatus, which uses 'scraping' instead of 'running').
export type RunStatus = 'queued' | 'running' | 'completed' | 'failed' | 'cancelled'

// --- agent runs (routes/agent_runs.py) ---

export interface AgentRunCreateRequest {
  tenant: string
  domain: string
  task: string
  tier?: Tier
  max_steps?: number
  output_schema?: Record<string, unknown> | null
}

export interface AgentRunCreateResponse {
  success: boolean
  run_id: string
}

export interface AgentStepOut {
  seq: number
  step_number: number
  evaluation_previous_goal: string | null
  memory: string | null
  next_goal: string | null
  actions: Record<string, unknown>[]
  action_results: string[]
  thinking: string | null
  duration_ms: number | null
  input_tokens: number | null
  output_tokens: number | null
  has_screenshot: boolean
  created_at: string
}

export interface AgentRunOut {
  run_id: string
  tenant: string
  task: string
  status: RunStatus
  current_step: number
  max_steps: number
  result: Record<string, unknown> | null
  error: string | null
  created_at: string
  started_at: string | null
  finished_at: string | null
}

export interface AgentRunStatusResponse {
  success: boolean
  data: AgentRunOut
  steps: AgentStepOut[]
  next: string | null
}

export interface AgentRunListResponse {
  success: boolean
  runs: AgentRunOut[]
  next: string | null
}

// --- recipes (routes/recipes.py) ---

export type RecipeHealthStatus = 'healthy' | 'degraded' | 'broken'
export type RecipeRunKind = 'build' | 'replay' | 'heal' | 'codegen' | 'onboard'

/**
 * A recipe run can also be *parked*, waiting for a person to settle a field
 * the agent could not. Kept separate from the shared `RunStatus` because an
 * agent run has no such state, and widening the shared union would tell every
 * consumer to handle a case that cannot happen to them.
 */
export type RecipeRunStatus = RunStatus | 'needs_input'
export type RecipeCodegenLanguage = 'python-playwright' | 'node-puppeteer' | 'python-requests-only'

export interface RecipeCreateRequest {
  tenant: string
  name: string
  url: string
  field_schema: Record<string, unknown>
  schedule_interval_seconds?: number | null
}

export interface RecipeCreateResponse {
  success: boolean
  recipe_id: string
  build_run_id: string
}

export interface RecipeRepeatSpec {
  option_locator: Record<string, unknown>
  max_iterations: number
  array_field: string
}

/**
 * One group, in whichever shape the recipe was written in.
 *
 * `field_groups` is a single JSON column carrying two different schemas. An
 * agent-*built* recipe writes the v1 shape (`field_locators` + `reveal_steps`);
 * one authored in the studio writes the v2 shape (`bindings` + `steps`), and
 * `save_document` stores that document's groups verbatim. Nothing converts
 * between them, so every reader has to expect either -- typing these as
 * required is what let a v2 recipe crash the detail page on
 * `reveal_steps.length`.
 */
export interface RecipeFieldGroup {
  group_id: string
  field_names: string[]
  // --- v1, from an agent build ---
  reveal_steps?: Record<string, unknown>[]
  field_locators?: Record<string, unknown>
  // --- v2, from the studio. `bindings` is keyed by field name, or by COLUMN
  // name for a table field, which is why it is not `Record<fieldName, ...>`.
  bindings?: Record<string, unknown>
  steps?: Record<string, unknown>[]
  repeat?: RecipeRepeatSpec | null
}

export interface RecipeOut {
  recipe_id: string
  tenant: string
  name: string
  url_pattern: string
  field_schema: Record<string, unknown>
  version: number
  global_setup: Record<string, unknown>[]
  field_groups: RecipeFieldGroup[]
  health_status: RecipeHealthStatus
  last_verified_at: string | null
  last_run_at: string | null
  schedule_interval_seconds: number | null
  created_at: string
  updated_at: string
  /**
   * The v2 document -- fields, bindings, steps. This IS the recipe;
   * `field_groups` above is the v1 shape and is empty for anything the
   * studio or the onboarding agent produced.
   */
  document?: Record<string, unknown> | null
}

export interface RecipeGetResponse {
  success: boolean
  data: RecipeOut
}

export interface RecipeListResponse {
  success: boolean
  recipes: RecipeOut[]
  next: string | null
}

export interface RecipeRunOut {
  run_id: string
  recipe_id: string
  tenant: string
  kind: RecipeRunKind
  status: RecipeRunStatus
  data: Record<string, unknown> | null
  field_failures: Record<string, unknown> | null
  error: string | null
  created_at: string
  started_at: string | null
  finished_at: string | null
  /** Set only while `status` is `needs_input`. */
  pending_asks?: PendingAsk[] | null
  /** What a long-running build is doing right now. */
  progress?: RunProgress | null
}

export interface RecipeRunResponse {
  success: boolean
  data: RecipeRunOut
}

export interface RecipeRunQueuedResponse {
  success: boolean
  run_id: string
}

export interface RecipeVersionOut {
  version: number
  diff_summary: string | null
  created_at: string
}

export interface RecipeVersionsResponse {
  success: boolean
  versions: RecipeVersionOut[]
}

export interface RecipeCodegenRequest {
  language: RecipeCodegenLanguage
}


// --- saving an authored v2 document (routes/recipes.py) ---

export type TemplateVisibility = 'private' | 'tenant' | 'public'

export interface RecipeSaveRequest {
  /** The full v2 document. */
  recipe: unknown
  schedule_interval_seconds?: number | null
  /** `pdp` | `plp` | `category` | `search` | `article` | ... free text. */
  page_type?: string | null
  template_visibility?: TemplateVisibility
}

export interface RecipeSaveResponse {
  success: boolean
  recipe_id: string
  version: number
  /** Lint findings that did not block the save. */
  warnings: string[]
}

export interface TemplateOut {
  recipe_id: string
  name: string
  domain: string | null
  page_type: string | null
  field_names: string[]
  version: number
  health_status: string
  updated_at: string
}

export interface TemplatesResponse {
  templates: TemplateOut[]
}


// --- extraction jobs: a marketplace recipe applied to submitted urls ---
//
// `TemplatesResponse` above is the browse half of the marketplace; this is the
// use half. A job is one submission of N urls; each url is one run.

export interface RecipeJobRequest {
  urls: string[]
  /** Addressable from the recipe as `{{meta.*}}` in step args and transforms. */
  metadata?: Record<string, unknown> | null
}

/**
 * `partial` is a real outcome for a batch, not a rounding of `failed`: some
 * URLs yielded and some did not, and the ones that did are still usable.
 */
export type RecipeJobStatus = 'running' | 'completed' | 'partial' | 'failed'

export interface RecipeJobOut {
  job_id: string
  recipe_id: string
  recipe_name: string
  /** Which version answered -- a recipe is healed and re-versioned under it. */
  recipe_version: number
  status: RecipeJobStatus
  total: number
  queued: number
  running: number
  completed: number
  failed: number
  created_at: string
  finished_at: string | null
}

/** The v2 engine's per-field verdict. A run can complete and still lose a field. */
export type RecipeFieldStatus = 'resolved' | 'fallback' | 'suspect' | 'empty' | 'failed'

export interface RecipeJobResultOut {
  run_id: string
  url: string
  status: RunStatus
  data: Record<string, unknown> | null
  field_status: Record<string, RecipeFieldStatus> | null
  outcome: string | null
  error: string | null
  finished_at: string | null
}

export interface RecipeJobQueuedResponse {
  success: boolean
  job_id: string
  queued: number
}

export interface RecipeJobResponse {
  success: boolean
  job: RecipeJobOut
  results: RecipeJobResultOut[]
}

export interface RecipeJobsResponse {
  success: boolean
  jobs: RecipeJobOut[]
}


// --- onboarding: build a recipe from a URL and a description of the data ---

export interface RecipeOnboardRequest {
  name: string
  url: string
  /** Plain English, e.g. "product name, price, sizes in stock, all image URLs". */
  description?: string
  /** A v2 `fields` object, when the caller already has one. Wins over `description`. */
  fields?: Record<string, unknown>
  /** More pages of the same kind. Two or more sharpen the derived `target.match`. */
  sample_urls?: string[]
}

export interface RecipeOnboardResponse {
  success: boolean
  recipe_id: string
  run_id: string
}

/** One thing a parked onboarding run needs a person to settle. */
export interface PendingAsk {
  field: string
  /**
   * `unresolved` was never found ("where is this?"); `rejected` was found but
   * judged to be the wrong thing ("you picked the breadcrumb, which is the
   * title?"). Different questions, so they are shown differently.
   */
  kind: 'unresolved' | 'rejected'
  reason: string
  /**
   * What the group's steps actually did. Without it an empty field behind a
   * reveal click is unattributable: the selector may be wrong, or the click may
   * never have run, and the two need opposite fixes.
   */
  step_trace: Array<Record<string, unknown>>
}

export interface RecipeResolution {
  field: string
  action: 'pick' | 'describe' | 'skip'
  /** `pick`: v2 locators, as the studio picker already produces them. */
  locators?: Array<Record<string, unknown>>
  /** `describe`: a hint fed to the selector agent, not used as a selector. */
  hint?: string
}

export interface RecipeAssistRequest {
  resolutions: RecipeResolution[]
}

export interface RecipeAssistResponse {
  success: boolean
  accepted: string[]
}


/** Live narration from a build that takes minutes. */
export interface RunProgress {
  phase?: 'exploring' | 'verifying'
  steps?: Array<{ n: number; goal: string; actions: string[]; found: string[] }>
  /** Fields bound so far. */
  found?: string[]
  /** Fields still being looked for. */
  remaining?: string[]
}
