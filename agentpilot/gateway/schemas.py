"""Pydantic mirror of `spi` types for HTTP-boundary validation only.

Every model is `extra="forbid"` (strict, friendly schemas). This module never
defines a shape `spi` doesn't already own -- it only adds JSON-Schema-shaped
validation on top of it.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# --- session lifecycle ---


class SessionOpenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant: str
    domain: str
    name: str
    tier: Literal["basic", "stealth", "enhanced", "auto"] = "auto"
    headful: bool = False
    block_popups: bool = False
    live_view: bool = False
    enable_cdp: bool = True


class SessionMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tier_used: str
    node_id: str
    duration_ms: float


class SessionOpenResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    metadata: SessionMetadata


# --- actions (closed, tagged union mirroring spi.actions) ---


class NavigateActionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["navigate"]
    url: str
    timeout_ms: int = 30_000


class GoBackActionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["go_back"]


class SnapshotActionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["snapshot"]
    viewport_only: bool = False
    max_nodes: int | None = None
    roles: list[str] | None = None
    with_bbox: bool = False


class ExtractActionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["extract"]
    format: Literal["markdown", "text", "html", "structured_data"] = "markdown"
    main_content: bool = True
    include_tags: list[str] = Field(default_factory=list)
    exclude_tags: list[str] = Field(default_factory=list)


class ScreenshotActionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["screenshot"]
    full_page: bool = False


class WaitActionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["wait"]
    ms: int | None = None
    ref: str | None = None


class ExecuteJsActionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["execute_js"]
    script: str


class ClickActionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["click"]
    ref: str
    all: bool = False


class FillActionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["fill"]
    ref: str
    text: str


class SelectOptionActionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["select_option"]
    ref: str
    values: list[str] = Field(default_factory=list)


class HoverActionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["hover"]
    ref: str


class PressActionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["press"]
    key: str


class ScrollActionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["scroll"]
    direction: Literal["up", "down", "left", "right"]
    ref: str | None = None


# --- tab management (mirrors spi.actions' NewTab/CloseTab/SwitchTab/ListTab) ---


class NewTabActionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["new_tab"]
    url: str | None = None


class CloseTabActionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["close_tab"]
    page_id: str


class SwitchTabActionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["switch_tab"]
    page_id: str


class ListTabsActionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["list_tabs"]


ActionIn = Annotated[
    NavigateActionIn
    | GoBackActionIn
    | SnapshotActionIn
    | ExtractActionIn
    | ScreenshotActionIn
    | WaitActionIn
    | ExecuteJsActionIn
    | ClickActionIn
    | FillActionIn
    | SelectOptionActionIn
    | HoverActionIn
    | PressActionIn
    | ScrollActionIn
    | NewTabActionIn
    | CloseTabActionIn
    | SwitchTabActionIn
    | ListTabsActionIn,
    Field(discriminator="type"),
]


class ExecuteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actions: list[ActionIn]
    page_id: str | None = None
    """Which tab this whole batch dispatches against; omitted/`None` means
    the session's current active tab -- see `spi.driver.BrowserDriver.
    execute`'s docstring."""


# --- scrape (one-shot: Navigate -> [actions] -> Extract(s) [-> Screenshot],
# composed server-side around an ephemeral identity -- see routes/scrape.py) ---


class ExtractConfigIn(BaseModel):
    """LLM-schema-driven structured extraction request -- see
    `spi.scrape.ExtractConfig`'s docstring for how this differs from the
    deterministic `"structured_data"` format, and for why this is
    `json_schema`, not `schema`."""

    model_config = ConfigDict(extra="forbid")
    json_schema: dict[str, Any] | None = None
    prompt: str | None = None


class ScrapeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant: str
    url: str
    tier: Literal["basic", "stealth", "enhanced", "auto"] = "auto"
    """Same not-yet-routed field as `SessionOpenRequest.tier` -- every scrape
    takes the same full-Patchright path today regardless of this value; see
    that model's field for the reasoning."""
    formats: list[Literal["markdown", "text", "html", "structured_data"]] = Field(
        default=["markdown"]
    )
    only_main_content: bool = True
    include_tags: list[str] = Field(default_factory=list)
    exclude_tags: list[str] = Field(default_factory=list)
    timeout_ms: int = 30_000
    wait_for_ms: int | None = None
    actions: list[ActionIn] = Field(default_factory=list)
    """Dispatched after navigate, before extraction -- e.g. dismiss a cookie
    banner. Reuses the exact same `ActionIn` union `/v1/sessions/{id}/execute`
    takes; `navigate`/`extract`/`screenshot` entries here are redundant with
    (and simply layered before) the ones `routes/scrape.py` appends itself,
    not rejected -- there's no reason to special-case that combination."""
    screenshot: bool = False
    full_page_screenshot: bool = False
    extract: ExtractConfigIn | None = None
    session_name: str | None = None
    """Anti-detection: opt into a warm, persistent browser profile for this
    `(tenant, domain, session_name)` instead of the default throwaway
    cookie-less profile. Repeat scrapes of the same site under the same name
    reuse accumulated cookies/state -- a returning-visitor signal that a
    fresh profile every call (a bot tell to WAFs like Akamai) lacks. Unset
    (the default) keeps the isolated one-shot behavior."""
    locale: str | None = None
    """Overrides the browser context's `navigator.language`/`Accept-Language`
    (e.g. `"en-US"`). Unset leaves Chrome's own default -- set it to a region
    plausible for the target site to avoid a locale-vs-target mismatch."""
    timezone_id: str | None = None
    """Overrides the browser's reported timezone (e.g. `"America/New_York"`).
    Should be coherent with `locale`. Unset leaves Chrome's own default."""


class ScrapeMetadataOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None
    status_code: int | None
    tier_used: str
    node_id: str
    duration_ms: float
    source_url: str


class DocumentOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document_id: str
    url: str
    markdown: str | None = None
    text: str | None = None
    html: str | None = None
    structured_data: dict[str, Any] | None = None
    links: list[str] = Field(default_factory=list)
    screenshot: str | None = None
    """Base64-encoded PNG, same encoding `ActionResultOut.screenshots` uses.
    Inline, not persisted -- `/v1/scrape` doesn't write a `documents` row
    (see `agentpilot.jobs.store`'s module docstring); a job-backed
    `/v1/crawl`/`/v1/batch/scrape` result instead carries a
    `screenshot_artifact_id` once an artifact store exists to upload to."""
    metadata: ScrapeMetadataOut | None = None
    error: str | None = None
    extract: dict[str, Any] | None = None
    extract_error: str | None = None


class ScrapeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    success: bool
    data: DocumentOut


# --- map (fast URL discovery, synchronous, no job queue -- routes/map.py) ---


class MapRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant: str
    url: str
    include_paths: list[str] = Field(default_factory=list)
    exclude_paths: list[str] = Field(default_factory=list)
    sitemap: Literal["skip", "include", "only"] = "include"
    include_subdomains: bool = False
    ignore_query_parameters: bool = False
    limit: int = 100_000
    search: str | None = None
    allow_external_links: bool = False
    filter_by_path: bool = True
    max_discovery_depth: int = Field(default=2, ge=0, le=10)
    timeout: int | None = Field(default=None, gt=0)
    """Overall discovery deadline in milliseconds; on expiry the route
    returns HTTP 408 (mirrors Firecrawl's `MapTimeoutError`)."""


class MapLinkOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str
    title: str | None = None
    description: str | None = None


class MapResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    success: bool
    links: list[MapLinkOut]
    warning: str | None = None


# --- crawl (async, Postgres-queue-backed -- routes/crawl.py, P4) ---


class ScrapeOptionsIn(BaseModel):
    """The subset of `ScrapeRequest` that makes sense nested inside a bulk
    `CrawlRequest`/`BatchScrapeRequest`: no `tenant`/`url`/`tier` (those
    belong to the outer request, or per-URL for batch), and deliberately no
    `actions` -- pre-extract interaction steps are much more a single-page
    `/v1/scrape` primitive; see `agentpilot.jobs.options_codec`'s module
    docstring for where that boundary is drawn and why."""

    model_config = ConfigDict(extra="forbid")
    formats: list[Literal["markdown", "text", "html", "structured_data"]] = Field(
        default=["markdown"]
    )
    only_main_content: bool = True
    include_tags: list[str] = Field(default_factory=list)
    exclude_tags: list[str] = Field(default_factory=list)
    timeout_ms: int = 30_000
    wait_for_ms: int | None = None
    screenshot: bool = False
    full_page_screenshot: bool = False
    extract: ExtractConfigIn | None = None


class WebhookIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str
    headers: dict[str, str] = Field(default_factory=dict)
    events: list[Literal["started", "page", "completed", "failed"]] = Field(
        default=["completed", "failed"]
    )


class CrawlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant: str
    url: str
    include_paths: list[str] = Field(default_factory=list)
    exclude_paths: list[str] = Field(default_factory=list)
    max_discovery_depth: int | None = None
    limit: int = 10_000
    allow_external_links: bool = False
    allow_subdomains: bool = False
    allow_backward_crawling: bool = False
    ignore_robots_txt: bool = False
    sitemap: Literal["skip", "include", "only"] = "include"
    deduplicate_similar_urls: bool = True
    ignore_query_parameters: bool = False
    delay_ms: int | None = None
    max_concurrency: int = 10
    scrape_options: ScrapeOptionsIn = Field(default_factory=ScrapeOptionsIn)
    webhook: WebhookIn | None = None


class CrawlCreateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    success: bool
    id: str
    url: str
    """A pollable `GET {this}` URL for the created job -- matches Firecrawl's
    `POST /v2/crawl` response shape."""
    webhook_secret: str | None = None
    """Plaintext, returned exactly once when `webhook` was set on the
    request -- never retrievable again, same "shown once" discipline as an
    API key's plaintext (`ApiKeyCreateOut.api_key`)."""


class CrawlStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    success: bool
    status: Literal["queued", "scraping", "completed", "failed", "cancelled"]
    total: int
    completed: int
    failed: int
    data: list[DocumentOut]
    next: str | None
    """Opaque keyset-pagination cursor -- pass back as `?after=` to fetch the
    next page; `None` means either no more pages, or the caller already
    reached the end of what's completed so far (poll again later for a
    still-running crawl)."""


# --- agent runs (async, Postgres-queue-backed -- routes/agent_runs.py) ---


class AgentRunCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant: str
    domain: str
    """Same role `SessionOpenRequest.domain` plays -- identity/proxy-pinning
    scope for the session this run opens, not necessarily the task's first
    URL (the agent may navigate anywhere the task requires)."""
    task: str
    tier: Literal["basic", "stealth", "enhanced", "auto"] = "auto"
    max_steps: int = 50
    output_schema: dict[str, Any] | None = None
    """Plain JSON Schema -- embedded into the `done` action's `extracted_data`
    field, same convention as `ExtractConfigIn.json_schema`."""


class AgentRunCreateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    success: bool
    run_id: str


class AgentStepOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    seq: int
    step_number: int
    evaluation_previous_goal: str | None
    memory: str | None
    next_goal: str | None
    actions: list[dict[str, Any]]
    action_results: list[str]
    created_at: str


class AgentRunOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str
    tenant: str
    task: str
    status: Literal["queued", "running", "completed", "failed", "cancelled"]
    current_step: int
    max_steps: int
    result: dict[str, Any] | None
    error: str | None
    created_at: str
    started_at: str | None
    finished_at: str | None


class AgentRunStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    success: bool
    data: AgentRunOut
    steps: list[AgentStepOut]
    next: str | None


# --- recipes (Phase 2: selector-generation & self-healing data collection --
# routes/recipes.py) ---


class RecipeCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant: str
    name: str
    url: str
    field_schema: dict[str, Any]
    """The caller's data contract -- `{field_name: {type: "scalar"|"array",
    description, item_schema?}}`, see `agentpilot.recipe.schema.FieldSpec`."""
    schedule_interval_seconds: float | None = None
    """`None` (the default) means on-demand only -- set to enqueue a
    `replay` run automatically every N seconds via `RecipeSchedulerLoop`."""


class RecipeCreateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    success: bool
    recipe_id: str
    build_run_id: str


class RecipeOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    recipe_id: str
    tenant: str
    name: str
    url_pattern: str
    field_schema: dict[str, Any]
    version: int
    global_setup: list[dict[str, Any]]
    field_groups: list[dict[str, Any]]
    health_status: Literal["healthy", "degraded", "broken"]
    last_verified_at: str | None
    last_run_at: str | None
    schedule_interval_seconds: float | None
    created_at: str
    updated_at: str


class RecipeGetResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    success: bool
    data: RecipeOut


class RecipeListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    success: bool
    recipes: list[RecipeOut]
    next: str | None


class RecipeRunOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str
    recipe_id: str
    tenant: str
    kind: Literal["build", "replay", "heal", "codegen"]
    status: Literal["queued", "running", "completed", "failed", "cancelled"]
    data: dict[str, Any] | None
    field_failures: dict[str, Any] | None
    error: str | None
    created_at: str
    started_at: str | None
    finished_at: str | None


class RecipeRunResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    success: bool
    data: RecipeRunOut


class RecipeRunQueuedResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    success: bool
    run_id: str


class RecipeVersionOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int
    diff_summary: str | None
    created_at: str


class RecipeVersionsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    success: bool
    versions: list[RecipeVersionOut]


class RecipeCodegenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    language: str = "python-playwright"


# --- action results ---


class BoundingBoxOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    x: float
    y: float
    width: float
    height: float


class SnapshotNodeOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    epoch: int
    ref: str
    role: str
    name: str
    children: list[SnapshotNodeOut] = Field(default_factory=list)
    bbox: BoundingBoxOut | None = None


class AXSnapshotOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    epoch: int
    root: SnapshotNodeOut


class ArtifactRefOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    artifact_id: str
    tenant: str
    kind: str
    size: int
    sha256: str


class TabInfoOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    page_id: str
    url: str
    title: str
    active: bool


class ActionResultOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    snapshots: list[AXSnapshotOut] = Field(default_factory=list)
    screenshots: list[str] = Field(default_factory=list)
    """Base64-encoded PNG bytes."""
    extracts: list[str] = Field(default_factory=list)
    js_returns: list[Any] = Field(default_factory=list)
    downloads: list[ArtifactRefOut] = Field(default_factory=list)
    tabs: list[list[TabInfoOut]] = Field(default_factory=list)
    """One entry per `list_tabs` action in the batch."""
    sequence_aborted: bool = False
    page_changed: bool = False


# --- sessions list (enterprise UI) ---


class SessionOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str
    tenant: str
    domain: str
    name: str
    tier: str
    headful: bool
    enable_cdp: bool
    node_id: str
    pid: int | None
    rss_mb: float | None
    state: Literal["active", "expired"]
    lease_expires_at: float | None
    """Unix timestamp; `None` when `state == "expired"` (no live lease)."""


class SessionListOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sessions: list[SessionOut]


# --- fleet (enterprise UI's nodes dashboard) ---


class NodeOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node_id: str
    addr: str | None
    started_at: float | None
    """Unix timestamp from `node:{id}`'s HSET, written once at the worker's boot."""
    live: bool
    """Whether `capacity:{id}` currently exists (its 10s TTL, refreshed every
    2s, is the actual liveness signal -- `live_nodes` SET membership alone is
    never trusted, same rule `place_session.lua`/`NodeReaper` follow). A node
    can appear here with `live=False` in the narrow window before the next
    `NodeReaper` cycle (every 5s) reaps it."""
    max_contexts: int | None
    active: int | None
    idle: int | None
    mem_used_pct: float | None
    cpu_used_pct: float | None


class NodeListOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    nodes: list[NodeOut]


# --- API keys (enterprise UI control plane, admin-gated) ---


class ApiKeyCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant: str
    name: str


class ApiKeyOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key_id: str
    tenant: str
    name: str
    prefix: str
    created_at: str
    last_used_at: str | None
    revoked_at: str | None


class ApiKeyCreateOut(ApiKeyOut):
    api_key: str
    """Plaintext, returned exactly once -- never retrievable again."""


class ApiKeyListOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    api_keys: list[ApiKeyOut]
