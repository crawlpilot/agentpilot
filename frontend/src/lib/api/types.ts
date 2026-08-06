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
}
export interface ExtractAction {
  type: 'extract'
  format?: 'markdown' | 'text' | 'html'
  main_content?: boolean
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
  extract?: Record<string, unknown> | null
  extract_error?: string | null
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
}

export interface MapLinkOut {
  url: string
  title?: string | null
  description?: string | null
}

export interface MapResponse {
  success: boolean
  links: MapLinkOut[]
}

// --- crawl (routes/crawl.py, async job -- POST creates, GET polls, DELETE cancels) ---

export interface ScrapeOptionsIn {
  formats?: ScrapeFormat[]
  only_main_content?: boolean
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

// --- recipes (routes/recipes.py) ---

export type RecipeHealthStatus = 'healthy' | 'degraded' | 'broken'
export type RecipeRunKind = 'build' | 'replay' | 'heal' | 'codegen'
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

export interface RecipeFieldGroup {
  group_id: string
  field_names: string[]
  reveal_steps: Record<string, unknown>[]
  field_locators: Record<string, unknown>
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
  status: RunStatus
  data: Record<string, unknown> | null
  field_failures: Record<string, unknown> | null
  error: string | null
  created_at: string
  started_at: string | null
  finished_at: string | null
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
