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

from agentpilot.spi.actions import Action, ExtractFormat


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
