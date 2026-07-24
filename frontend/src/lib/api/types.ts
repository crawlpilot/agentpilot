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
  | 'INTERNAL_ERROR'

export interface ApiErrorBody {
  success: false
  code: ErrorCode
  error: string
  details?: unknown
}
