"""Pydantic mirror of `spi` types for HTTP-boundary validation only.

Every model is `extra="forbid"` (strict, friendly schemas). This module never
defines a shape `spi` doesn't already own -- it only adds JSON-Schema-shaped
validation on top of it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field

from crawlpilot.tiers import TierName
from crawlpilot.tools import CATALOG
from crawlpilot.tools.spec import union_of
from crawlpilot.wire import (
    ActionResultWire,
    BoundingBoxWire,
    DownloadWire,
    RefInfoWire,
    SnapshotWire,
    TabInfoWire,
)

# --- session lifecycle ---


class SessionOpenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant: str
    domain: str
    name: str
    tier: TierName = "auto"
    headful: bool = False
    block_popups: bool = False
    live_view: bool = False
    enable_cdp: bool = True
    # No per-session `extensions` allowlist, deliberately -- see
    # `ScrapeRequest.extensions`, which has one. An interactive session's
    # extension hooks reach it through the *driver*, which is constructed once
    # per process (`wiring.py`'s `PatchrightDriver(block_hooks=...)`), so they
    # are a property of the deployment rather than of a session. Offering the
    # field here would accept it and silently do nothing. Making it work means
    # threading hooks through `open_interactive_session` to the context, which
    # is a change to how the driver is built, not to this schema.


class SessionMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tier_used: str
    node_id: str
    duration_ms: float


class SessionOpenResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    metadata: SessionMetadata


# --- actions ---
#
# Generated from `crawlpilot.tools.CATALOG`, which is now the single place a
# browser verb is declared (plan D5). These 17 models and their union used to be
# written out by hand here, mirroring the `spi.actions` dataclasses, with a
# third copy in `agent.actions` and a 17-entry converter table in
# `action_conversion.py` -- four files to edit for one new verb.
#
# The generated union is **byte-identical** to the hand-written one it replaced;
# `tests/test_tools_registry.py` asserts that against a golden schema captured
# before the change, so the published OpenAPI is unaffected.

_WIRE_MODELS = {spec.name: spec.wire_model() for spec in CATALOG}
globals().update({model.__name__: model for model in _WIRE_MODELS.values()})


class ExtensionActionIn(BaseModel):
    """A verb this *build* does not know, deferred to the server's registry.

    The union above is built from `CATALOG` at import -- before any
    `ExtensionRegistry` exists -- so a verb contributed by a `ToolMount`
    extension could never be dispatched over HTTP at all. The namespacing that
    exists precisely so a third party can ship `walmart.solve_wall` had nothing
    that could accept one.

    Making the *published* union per-deployment is not the fix: routers are
    built at import, and an OpenAPI document that changes with the installed
    extensions is worse to consume, not better. So the schema keeps documenting
    what this build ships, and anything else falls through to here and is
    validated against the live registry in `action_conversion.to_spi_action` --
    where the verb's own `ToolSpec` supplies the schema, `domains` is honoured,
    and an unknown name produces a message naming what *is* available.

    Two doors, matching the two kinds of caller: typed built-ins for anyone
    coding against the published schema, this for extension verbs and for a
    client running a release behind its server.
    """

    model_config = ConfigDict(extra="allow")
    type: str


if TYPE_CHECKING:  # names the generator produces, spelled out for type checkers
    ActionIn = Any
else:
    # An outer, *non*-discriminated union: pydantic tries the discriminated
    # built-ins first and falls back to the passthrough. A discriminated union
    # cannot carry a catch-all -- that is what the extra nesting buys.
    ActionIn = Union[union_of(list(_WIRE_MODELS.values())), ExtensionActionIn]  # noqa: UP007


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
    tier: TierName = "auto"
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
    extensions: list[str] | None = None
    """Which of the deployment's extensions to run this scrape with, by name.

    An `Extension` is *code* -- hooks that rewrite a URL, warm a site up, or
    resolve a detected wall -- so it cannot travel over HTTP. Site knowledge
    reaches a worker as a `pip install` into its image (the
    `crawlpilot.extensions` entry-point group); this selects among what is
    already there.

    `None` means the deployment's normal set, so nothing changes for existing
    callers. `[]` means run with none, which is how you see a page exactly as it
    is served rather than as an extension repaired it. `GET /v1/capabilities`
    lists the names available here.

    Scrape-only: an ephemeral scrape takes its hooks per call
    (`run_ephemeral_scrape(block_hooks=...)`), whereas an interactive session
    inherits them from the process-wide driver -- see `SessionOpenRequest`."""


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
    extract: dict[str, Any] | list[Any] | None = None
    """A list when the caller's `extract.json_schema` had an array at its root
    -- see `crawlpilot.spi.scrape.Document.extract`."""
    extract_error: str | None = None
    extract_warning: str | None = None
    """Non-fatal degradation of an extraction that still produced a result --
    today, input truncated because the page exceeded the model's budget."""


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
    tier: TierName = "auto"
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
    thinking: str | None
    duration_ms: int | None
    input_tokens: int | None
    output_tokens: int | None
    has_screenshot: bool
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


class AgentRunListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    success: bool
    runs: list[AgentRunOut]
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
#
# Not defined here any more. These were hand-written mirrors of
# `spi.actions.ActionResult`, and being hand-written they had drifted: the
# response omitted `values`, `readouts`, `verifications`, `pdfs`, `frames`,
# `page_title`, `status_code`, `soft_verdict`, `soft_weight` and `dialog`.
#
# `values` in particular is what `api.BrowserSession`'s getters read, so its
# absence made this HTTP API strictly weaker than the library it fronts -- no
# client on the far side could implement `get_text`, `is_visible`, `get_count`,
# `dropdown_options`, `pdf()` or `list_frames()` at all.
#
# `crawlpilot.wire` projects them from the dataclass instead, the same way
# `tools.ToolSpec` already projects the *request* union from `tools/catalog.py`.
# Requests were a projection and responses were a copy; now both are
# projections, and a field added to `ActionResult` reaches the wire with no edit
# in this file. The names are re-exported so routes and any importer are
# unchanged.
#
# `FusedTreeOut` is kept as an alias for `SnapshotWire`: the wire field is
# `snapshots` now (it never carried a fused tree -- always the serialized
# `{llm_text, refs}` view), and `crawlpilot.wire` still *accepts* `fused_trees`
# on input, so no existing payload stops decoding.
#
# Split on TYPE_CHECKING for the same reason `ActionIn` above is: these are
# built by `create_model` at import, so a type checker sees a variable rather
# than a class and rejects it in an annotation.
if TYPE_CHECKING:
    ActionResultOut = Any
    BoundingBoxOut = Any
    RefInfoOut = Any
    TabInfoOut = Any
    ArtifactRefOut = Any
    FusedTreeOut = Any
else:
    ActionResultOut = ActionResultWire
    BoundingBoxOut = BoundingBoxWire
    RefInfoOut = RefInfoWire
    TabInfoOut = TabInfoWire
    ArtifactRefOut = DownloadWire
    FusedTreeOut = SnapshotWire


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
