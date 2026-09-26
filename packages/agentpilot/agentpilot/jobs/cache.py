"""Scrape-result cache: serve an unchanged page from Postgres instead of
launching a browser for it again.

This is the clearest cost line in the system. Every `/v1/scrape` and every
`crawl_tasks` row currently opens an ephemeral browser context, navigates,
renders and extracts, even when the same tenant asked for the same URL with the
same options ninety seconds ago. A crawl re-run after a caller fixes one
`include_paths` entry pays for the whole site a second time.

**Why this lives in `agentpilot`, not `crawlpilot`.** The same reason
`agentpilot.crawl.types.CrawlOptions` does: the browser library has no opinion
about whether a result was worth keeping, and `crawlpilot` may not import
`psycopg` at all (its import-linter contract forbids it). So `CacheMode` and
`CachePolicy` are platform types, the cache is consulted *around*
`run_ephemeral_scrape` rather than inside it, and nothing in the engine changes.

**Conservative by construction.** A cache that returns a subtly wrong page is
far worse than no cache, so `is_cacheable()` refuses every request whose result
depends on something the key cannot faithfully capture -- see its docstring for
the three cases and why each one is excluded rather than approximated.

`CacheMode`'s member names are crawl4ai's (`crawl4ai/cache_context.py`), which
are in turn HTTP's: the five modes are the useful combinations of "may read"
and "may write", and having them named the same way as the library this was
adapted from costs nothing and helps anyone moving between the two.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

import structlog

from crawlpilot.spi.scrape import Document, DocumentMetadata, ScrapeOptions

log = structlog.get_logger(__name__)

CacheMode = Literal["enabled", "bypass", "read_only", "write_only", "disabled"]
"""`enabled` reads and writes. `bypass` skips the read but still refreshes the
entry, which is how a caller forces one URL without evicting anything.
`read_only` serves a hit but never writes (a backfill run that must not pollute
the cache with its own results). `write_only` is its mirror -- always fetch,
always store -- which is how a cache is warmed. `disabled` ignores the cache
entirely, as though the table did not exist."""

DEFAULT_MAX_AGE_MS = 3_600_000
"""One hour. Deliberately short: this is a scraping service, and the common
reason to scrape the same URL twice is that the caller expects it to have
changed. A caller who knows their target is static passes a larger
`max_age_ms`; one who needs the live page passes `cache_mode="bypass"`."""


@dataclass(frozen=True)
class CachePolicy:
    mode: CacheMode = "enabled"
    max_age_ms: int = DEFAULT_MAX_AGE_MS

    @property
    def may_read(self) -> bool:
        return self.mode in ("enabled", "read_only")

    @property
    def may_write(self) -> bool:
        return self.mode in ("enabled", "bypass", "write_only")


# Option fields that change what a scrape *returns*. Anything absent from this
# list is either operational (`timeout_ms` decides whether the scrape finishes,
# not what it says) or handled by `is_cacheable` below.
_KEYED_OPTION_FIELDS = (
    "formats",
    "only_main_content",
    "include_tags",
    "exclude_tags",
    "relevance_query",
    "citations",
    "wait_for_ms",
    "block_images",
    "block_resource_types",
    "block_hosts",
)


def is_cacheable(options: ScrapeOptions, *, session_name: str | None = None) -> bool:
    """Whether a result for these options may be cached at all.

    Three exclusions, each because the alternative is a wrong answer rather
    than a slow one:

    1. **Pre-extract actions.** `options.actions` mutates the page before
       extraction -- dismiss a banner, click "load more", fill a form. Serving a
       cached result would silently skip the interaction the caller asked for,
       and a caller cannot tell that from the interaction having had no effect.
       Actions could in principle be hashed into the key, but a cache hit on an
       identical interaction sequence is rare enough that carrying the risk buys
       almost nothing.
    2. **A named warm session.** `session_name` opts into a persistent profile
       whose accumulated cookies and storage are the point; two scrapes under
       the same name are deliberately *not* the same request, because the second
       one is a returning visitor. Caching across that erases the behaviour the
       caller is paying for.
    3. **Screenshots.** A screenshot travels base64-inline on the scrape path
       and as an artifact id on the job path (see `Document
       .screenshot_artifact_id`). Neither survives this table as it stands, so a
       hit would return a document whose screenshot silently went missing.
    """

    if options.actions:
        return False
    if session_name:
        return False
    if options.screenshot:
        return False
    return True


def is_cacheable_result(document: Document) -> bool:
    """Whether a finished scrape may be stored.

    `is_cacheable` above judges the *request*; this judges what came back. Two
    refusals, both "an error is a statement about one attempt, not about the page":

    * `document.error` -- the page did not load.
    * `document.extract_error` -- the page loaded but the LLM extraction failed.

    The second one is the whole reason this is a function rather than an `if` at
    each call site. A scrape with `extract` set whose model call times out returns
    a *successful* document: markdown intact, `error` unset, `extract` null and
    `extract_error` populated. Guarding only on `error` therefore stores that
    failure and serves it back for the whole TTL -- so one transient rate-limit
    turns into an hour of a caller's extraction being deterministically empty,
    with no further model calls attempted and nothing in the logs to say why.

    `extract_warning` does *not* block a write: that document has a real result
    (truncated input), and the warning travels with it.
    """

    if document.error is not None:
        return False
    return document.extract_error is None


def cache_key(
    *,
    tenant: str,
    url: str,
    options: ScrapeOptions,
    variant: dict[str, Any] | None = None,
) -> str:
    """A stable digest of everything that changes the result.

    `tenant` is part of the key, not a column filtered on afterwards. That is
    the deliberate choice: a shared cache across tenants would be the larger
    cost win, and it is also how one tenant's scrape of a page behind their own
    cookies, proxy or locale becomes another tenant's response. Sharing can be
    added later for provably anonymous scrapes; it cannot be un-leaked.

    `variant` carries the request-level fields that sit on `ScrapeRequest`
    rather than `ScrapeOptions` but still change what the server returns --
    `locale`, `timezone_id`, `tier`, `extensions`. Callers pass them explicitly
    because this module cannot see them, and a key that quietly ignored them
    would serve a French page to a caller who asked for `en-US`.
    """

    payload: dict[str, Any] = {
        "v": 1,
        "tenant": tenant,
        "url": url,
    }
    for field in _KEYED_OPTION_FIELDS:
        payload[field] = _canonical(getattr(options, field))
    payload["extract"] = (
        {
            "json_schema": options.extract.json_schema,
            "prompt": options.extract.prompt,
        }
        if options.extract is not None
        else None
    )
    payload["variant"] = _canonical(variant or {})

    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.blake2b(encoded.encode("utf-8"), digest_size=24).hexdigest()


def _canonical(value: Any) -> Any:
    """Tuples become lists so JSON round-trips them, and `formats` is sorted:
    asking for `["markdown", "html"]` and `["html", "markdown"]` produces the
    same document, so it must produce the same key."""

    if isinstance(value, tuple | list):
        return sorted(str(item) for item in value)
    if isinstance(value, dict):
        return {key: _canonical(val) for key, val in sorted(value.items())}
    return value


# --- Document <-> JSON, for the cache's `document` column ---------------------


def document_to_json(document: Document) -> dict[str, Any]:
    """Hand-written rather than `dataclasses.asdict`, for the same reason
    `options_codec` is: a field added to `Document` should fail a test here
    loudly, not start silently round-tripping through a cache that was never
    reviewed for it. `document_id` is deliberately excluded -- it identifies
    *this* scrape, and a cache hit is a different scrape of the same page, so
    the reader mints a fresh one."""

    metadata = document.metadata
    return {
        "url": document.url,
        "markdown": document.markdown,
        "fit_markdown": document.fit_markdown,
        "text": document.text,
        "html": document.html,
        "entities": document.entities,
        "tables": document.tables,
        "structured_data": document.structured_data,
        "raw_html": document.raw_html,
        "links": list(document.links),
        "error": document.error,
        "extract": document.extract,
        "extract_error": document.extract_error,
        "extract_warning": document.extract_warning,
        "metadata": (
            {
                "title": metadata.title,
                "status_code": metadata.status_code,
                "tier_used": metadata.tier_used,
                "node_id": metadata.node_id,
                "duration_ms": metadata.duration_ms,
                "source_url": metadata.source_url,
            }
            if metadata is not None
            else None
        ),
    }


def document_from_json(data: dict[str, Any], *, document_id: str) -> Document:
    metadata = data.get("metadata")
    return Document(
        document_id=document_id,
        url=data.get("url", ""),
        markdown=data.get("markdown"),
        fit_markdown=data.get("fit_markdown"),
        text=data.get("text"),
        html=data.get("html"),
        entities=data.get("entities"),
        tables=data.get("tables"),
        structured_data=data.get("structured_data"),
        raw_html=data.get("raw_html"),
        links=tuple(data.get("links") or ()),
        screenshot_artifact_id=None,
        metadata=(
            DocumentMetadata(
                title=metadata.get("title"),
                status_code=metadata.get("status_code"),
                tier_used=metadata.get("tier_used", ""),
                node_id=metadata.get("node_id", ""),
                duration_ms=metadata.get("duration_ms", 0.0),
                source_url=metadata.get("source_url", data.get("url", "")),
            )
            if metadata
            else None
        ),
        error=data.get("error"),
        extract=data.get("extract"),
        extract_error=data.get("extract_error"),
        extract_warning=data.get("extract_warning"),
    )


class ScrapeCacheProtocol(Protocol):
    async def get(self, key: str, *, max_age_ms: int) -> Document | None: ...

    async def put(self, key: str, *, tenant: str, url: str, document: Document) -> None: ...


class NullScrapeCache:
    """Always misses, never stores. What a deployment without
    `AGENTPILOT_DATABASE_URL` gets, so every call site can hold a cache
    unconditionally instead of branching on whether one exists."""

    async def get(self, key: str, *, max_age_ms: int) -> Document | None:
        return None

    async def put(self, key: str, *, tenant: str, url: str, document: Document) -> None:
        return None


class PostgresScrapeCache:
    """One row per `(key)` in `scrape_cache`, upserted on write.

    Freshness is evaluated at *read* time against the caller's `max_age_ms`
    rather than by storing a per-row expiry, because two callers legitimately
    disagree about how old is too old: a nightly archival crawl is happy with a
    week-old page and a price checker is not. One row can serve both.
    """

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    @classmethod
    async def connect(cls, database_url: str) -> PostgresScrapeCache:
        from psycopg_pool import AsyncConnectionPool

        pool = AsyncConnectionPool(
            database_url, min_size=1, max_size=5, open=False, kwargs={"autocommit": True}
        )
        await pool.open(wait=True, timeout=10.0)
        return cls(pool)

    async def close(self) -> None:
        await self._pool.close()

    async def get(self, key: str, *, max_age_ms: int) -> Document | None:
        """A hit, or `None` for a miss *or any failure*.

        Fail-open, and not as a nicety: a cache is an optimization, and an
        optimization that can turn a working scrape into a 500 is a liability. The
        concrete case is a deployment whose `alembic upgrade head` has not run
        since this table was added -- `scrape_cache` does not exist, every lookup
        raises `UndefinedTable`, and without this every single scrape fails. Same
        for a connection-pool timeout under load, which is exactly when the cache
        matters most and exactly when it is most likely to be unavailable.
        """

        import uuid

        from psycopg.rows import dict_row

        try:
            async with self._pool.connection() as conn:
                async with conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute(
                        "SELECT document, created_at FROM scrape_cache WHERE cache_key = %s",
                        (key,),
                    )
                    row = await cur.fetchone()
        except Exception as exc:  # noqa: BLE001 -- degrade to a miss, never raise
            log.warning("scrape_cache.read_failed", error=str(exc))
            return None
        if row is None:
            return None
        age_ms = (datetime.now(UTC) - row["created_at"]).total_seconds() * 1000
        if age_ms > max_age_ms:
            return None
        return document_from_json(row["document"], document_id=str(uuid.uuid4()))

    async def put(self, key: str, *, tenant: str, url: str, document: Document) -> None:
        """Store, or give up quietly. Same reasoning as `get`: failing to populate
        a cache costs the next caller a scrape, while raising costs this one their
        response for no reason at all."""


        try:
            await self._put(key, tenant=tenant, url=url, document=document)
        except Exception as exc:  # noqa: BLE001 -- see the docstring
            log.warning("scrape_cache.write_failed", error=str(exc))

    async def _put(self, key: str, *, tenant: str, url: str, document: Document) -> None:
        from psycopg.types.json import Jsonb

        async with self._pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO scrape_cache (cache_key, tenant, url, document, created_at)
                VALUES (%s, %s, %s, %s, now())
                ON CONFLICT (cache_key) DO UPDATE
                    SET document = EXCLUDED.document, created_at = now()
                """,
                (key, tenant, url, Jsonb(document_to_json(document))),
            )
