"""One-shot scrape: driver-agnostic request/result shapes for `/v1/scrape`
and the crawl/batch-scrape pipelines built on top of it.

`ScrapeOptions.actions` reuses `spi.actions.Action` rather than inventing a
parallel action type -- a scrape's pre-extract steps (accept a cookie banner,
click "load more") are dispatched through the exact same batched-execute
machinery `/v1/sessions/{id}/execute` already uses, just composed server-side
around a single `NavigateAction`/`ExtractAction`/`ScreenshotAction` sequence
instead of caller-driven round trips (see `gateway/routes/scrape.py`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agentpilot.spi.actions import Action, ExtractFormat


@dataclass
class ExtractConfig:
    """LLM-schema-driven structured extraction (`agentpilot.llm.schema_extract`)
    -- a single model call over the scrape's markdown, distinct from the
    deterministic `structured_data` format (`agentpilot.extraction
    .structured_data`, no LLM). `json_schema` is plain JSON Schema, not a
    custom DSL -- it's handed directly to an OpenAI-compatible
    `response_format.json_schema.schema` parameter, no translation layer
    needed. Named `json_schema`, not `schema`, to avoid shadowing Pydantic's
    own `BaseModel.schema()` on the gateway-side mirror of this type."""

    json_schema: dict[str, Any] | None = None
    prompt: str | None = None


@dataclass
class ScrapeOptions:
    formats: tuple[ExtractFormat, ...] = ("markdown",)
    only_main_content: bool = True
    include_tags: tuple[str, ...] | None = None
    exclude_tags: tuple[str, ...] | None = None
    timeout_ms: int = 30_000
    wait_for_ms: int | None = None
    actions: tuple[Action, ...] = ()
    screenshot: bool = False
    full_page_screenshot: bool = False
    extract: ExtractConfig | None = None
    block_images: bool = False
    """Shorthand: block image/media/font requests (a common speed/bandwidth win
    -- a scraper rarely needs pixels). Expands into `block_resource_types` at the
    driver. Off by default (no interception, unchanged behavior)."""
    block_resource_types: tuple[str, ...] = ()
    """Playwright `request.resource_type` values to abort during the scrape
    (e.g. `("image", "media", "font", "stylesheet")`). Never include `script`,
    `xhr`, `fetch`, or `document` -- JS-rendered content and Akamai's sensor
    depend on them. Ported from Pulsar's `Network.setBlockedURLs` /
    `BrowserSettings.blockImages`, via Patchright's `context.route`. Empty by
    default (no interception)."""
    block_hosts: tuple[str, ...] = ()
    """Substrings matched against each request URL; a match aborts the request
    (analytics/beacon hosts, third-party trackers). Empty by default."""


@dataclass
class DocumentMetadata:
    """Mirrors `SessionMetadata`'s field names (`tier_used`/`node_id`/
    `duration_ms`) deliberately -- same "how was this served" shape, one per
    document instead of one per session."""

    title: str | None
    status_code: int | None
    tier_used: str
    node_id: str
    duration_ms: float
    source_url: str


@dataclass
class Document:
    """The scrape/crawl/batch-scrape output for one URL. `document_id` is
    always set (even for a synchronous `/v1/scrape` call that never persists
    a row) so callers have a stable handle regardless of whether this
    instance ever passes through `agentpilot.jobs.store.PostgresJobStore`."""

    document_id: str
    url: str
    markdown: str | None = None
    text: str | None = None
    html: str | None = None
    structured_data: dict[str, Any] | None = None
    """JSON-LD/meta-OG-Twitter-DC/Next.js-Nuxt hydration-state bundle, set
    when `formats` includes `"structured_data"` -- see `agentpilot.extraction
    .structured_data`. Deterministic, no LLM; see `extract` for the separate
    LLM-schema-driven counterpart."""
    raw_html: str | None = None
    """Always `None` today -- `spi.actions.ExtractAction`'s `html` format
    already returns `page.content()` unmodified (see that class's
    docstring), so there is no distinct "cleaned vs. raw" html the engine
    can currently produce. Kept as a field (not removed) since a future
    `main_content`-cleaned `html` alongside a genuinely raw variant is a
    plausible engine addition, and this is where it would land."""
    links: tuple[str, ...] = field(default_factory=tuple)
    screenshot_artifact_id: str | None = None
    metadata: DocumentMetadata | None = None
    error: str | None = None
    extract: dict[str, Any] | None = None
    """The LLM's schema-shaped output, set only when `ScrapeOptions.extract`
    was set. Separate from `structured_data` (deterministic, no LLM) and
    from the top-level `error` (a failed LLM call shouldn't null out an
    otherwise-successful markdown/html scrape) -- see `extract_error`."""
    extract_error: str | None = None
    """Set instead of `extract` when the LLM call fails (including
    `AGENTPILOT_LLM_API_KEY` unset) -- never raised through to fail the
    whole scrape."""
