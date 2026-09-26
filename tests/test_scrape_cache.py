"""`agentpilot.jobs.cache` -- the key, the safety exclusions, and the document
round trip. Pure functions only; `PostgresScrapeCache` needs a database and is
covered by `test_jobs_store.py`'s idiom where it is exercised at all.

The key tests are the load-bearing ones here. A key that collides across two
requests that should differ is not a slow cache, it is a cache that returns the
wrong page -- so each field that changes the result gets an explicit test that
changing it changes the key, and `timeout_ms` gets one proving it does not.
"""

from __future__ import annotations

import dataclasses

import pytest

from agentpilot.crawl.types import CacheMode as CrawlTypesCacheMode
from agentpilot.jobs.cache import (
    CacheMode,
    CachePolicy,
    cache_key,
    document_from_json,
    document_to_json,
    is_cacheable,
    is_cacheable_result,
)
from crawlpilot.spi.actions import ClickAction
from crawlpilot.spi.scrape import Document, DocumentMetadata, ExtractConfig, ScrapeOptions


def _key(**kwargs: object) -> str:
    base: dict[str, object] = {"tenant": "t1", "url": "https://example.com/a"}
    base.update(kwargs)
    options = base.pop("options", ScrapeOptions())
    return cache_key(**base, options=options)  # type: ignore[arg-type]


# ------------------------------------------------------------------ the key


def test_the_same_request_produces_the_same_key() -> None:
    options = ScrapeOptions(formats=("markdown", "html"), relevance_query="refunds")
    assert _key(options=options) == _key(options=options)


def test_a_different_tenant_never_shares_a_key() -> None:
    """The cache is per-tenant by construction. Sharing would be the larger cost
    win and is also how one tenant's cookied/proxied page becomes another
    tenant's response -- it cannot be un-leaked, so it is not the default."""

    assert _key(tenant="t1") != _key(tenant="t2")


def test_a_different_url_never_shares_a_key() -> None:
    assert _key(url="https://example.com/a") != _key(url="https://example.com/b")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("formats", ("markdown", "fit_markdown")),
        ("only_main_content", False),
        ("include_tags", ("article",)),
        ("exclude_tags", ("nav",)),
        ("relevance_query", "warranty"),
        ("citations", True),
        ("wait_for_ms", 500),
        ("block_images", True),
        ("block_resource_types", ("image",)),
        ("block_hosts", ("analytics.example.com",)),
    ],
)
def test_every_output_affecting_option_changes_the_key(field: str, value: object) -> None:
    assert _key(options=ScrapeOptions()) != _key(
        options=ScrapeOptions(**{field: value})  # type: ignore[arg-type]
    )


def test_timeout_ms_does_not_change_the_key() -> None:
    """`timeout_ms` decides whether a scrape finishes, not what it says. Keying
    on it would split the cache into per-timeout buckets that all hold the same
    page."""

    assert _key(options=ScrapeOptions(timeout_ms=1_000)) == _key(
        options=ScrapeOptions(timeout_ms=60_000)
    )


def test_format_order_does_not_change_the_key() -> None:
    """`["markdown", "html"]` and `["html", "markdown"]` produce the same
    document, so they must hit the same entry."""

    assert _key(options=ScrapeOptions(formats=("markdown", "html"))) == _key(
        options=ScrapeOptions(formats=("html", "markdown"))
    )


def test_the_llm_extract_config_changes_the_key() -> None:
    a = ScrapeOptions(extract=ExtractConfig(json_schema={"type": "object"}))
    b = ScrapeOptions(extract=ExtractConfig(json_schema={"type": "array"}))
    assert _key(options=a) != _key(options=b)
    assert _key(options=a) != _key(options=ScrapeOptions())


def test_the_request_level_variant_changes_the_key() -> None:
    """`locale`/`timezone_id`/`tier`/`extensions` live on `ScrapeRequest`, not
    `ScrapeOptions`, but they change what the server returns. A key that ignored
    them would serve an `en-US` page to a caller who asked for `ja-JP`."""

    assert _key(variant={"locale": "en-US"}) != _key(variant={"locale": "ja-JP"})
    assert _key(variant={"tier": "auto"}) != _key(variant={"tier": "stealth"})
    assert _key(variant=None) != _key(variant={"locale": "en-US"})


def test_variant_key_order_does_not_matter() -> None:
    assert _key(variant={"locale": "en-US", "tier": "auto"}) == _key(
        variant={"tier": "auto", "locale": "en-US"}
    )


# --------------------------------------------------------------- exclusions


def test_a_request_with_pre_extract_actions_is_never_cached() -> None:
    """Serving a hit would silently skip the interaction the caller asked for,
    and they could not tell that from the interaction having had no effect."""

    assert not is_cacheable(ScrapeOptions(actions=(ClickAction(selector="#accept"),)))


def test_a_named_warm_session_is_never_cached() -> None:
    """Two scrapes under one `session_name` are deliberately not the same
    request -- the second is a returning visitor, which is the behaviour being
    paid for."""

    assert not is_cacheable(ScrapeOptions(), session_name="walmart-warm")


def test_a_screenshot_request_is_never_cached() -> None:
    assert not is_cacheable(ScrapeOptions(screenshot=True))


def test_an_ordinary_request_is_cacheable() -> None:
    assert is_cacheable(ScrapeOptions(formats=("markdown",)))


# ------------------------------------------------------------------ policy


@pytest.mark.parametrize(
    ("mode", "may_read", "may_write"),
    [
        ("enabled", True, True),
        ("bypass", False, True),
        ("read_only", True, False),
        ("write_only", False, True),
        ("disabled", False, False),
    ],
)
def test_cache_modes_permit_what_they_say(mode: str, may_read: bool, may_write: bool) -> None:
    policy = CachePolicy(mode=mode)  # type: ignore[arg-type]
    assert policy.may_read is may_read
    assert policy.may_write is may_write


def test_the_two_cache_mode_literals_stay_identical() -> None:
    """`crawl.types` re-declares `CacheMode` rather than importing it, to stay a
    leaf module. That is only safe while the two agree."""

    assert CacheMode == CrawlTypesCacheMode


# ------------------------------------------------------------- round trip


def _document() -> Document:
    return Document(
        document_id="original-id",
        url="https://example.com/a",
        markdown="# page",
        tables=[{"headers": ["A"], "rows": [["1"]], "caption": None, "summary": None}],
        fit_markdown="# page, filtered",
        text="page",
        html="<h1>page</h1>",
        entities={"email": ["hi@example.com"]},
        structured_data={"json_ld": []},
        raw_html=None,
        links=("https://example.com/b",),
        screenshot_artifact_id="art-1",
        metadata=DocumentMetadata(
            title="Page",
            status_code=200,
            tier_used="auto",
            node_id="n1",
            duration_ms=12.5,
            source_url="https://example.com/a",
        ),
        error=None,
        extract={"price": 10},
        extract_error=None,
        extract_warning="truncated",
    )


def test_the_document_round_trips_through_json() -> None:
    restored = document_from_json(document_to_json(_document()), document_id="new-id")
    original = _document()

    assert restored.markdown == original.markdown
    assert restored.fit_markdown == original.fit_markdown
    assert restored.text == original.text
    assert restored.html == original.html
    assert restored.entities == original.entities
    assert restored.tables == original.tables
    assert restored.structured_data == original.structured_data
    assert restored.links == original.links
    assert restored.extract == original.extract
    assert restored.extract_warning == original.extract_warning
    assert restored.metadata is not None
    assert restored.metadata.title == "Page"
    assert restored.metadata.status_code == 200
    assert restored.metadata.duration_ms == 12.5


def test_a_cache_hit_gets_a_fresh_document_id() -> None:
    """`document_id` identifies one scrape. A hit is a different scrape of the
    same page, so reusing the stored id would hand two responses the same
    handle."""

    restored = document_from_json(document_to_json(_document()), document_id="new-id")
    assert restored.document_id == "new-id"


def test_the_screenshot_artifact_id_is_deliberately_dropped() -> None:
    """`is_cacheable` already refuses screenshot requests, so a stored document
    should never have one -- and if one slips through, returning an artifact id
    whose artifact was never written for this response is worse than returning
    nothing."""

    restored = document_from_json(document_to_json(_document()), document_id="new-id")
    assert restored.screenshot_artifact_id is None


def test_every_document_field_is_accounted_for() -> None:
    """The guard against the real failure mode of a hand-written codec: a field
    added to `Document` that nobody remembers to carry through the cache, so a
    hit silently returns it as `None`.

    A new field must be either round-tripped or named in the excluded set below,
    with a reason.
    """

    excluded = {
        "document_id",  # minted fresh per read -- see the test above
        "screenshot_artifact_id",  # deliberately dropped -- see the test above
    }
    serialized = set(document_to_json(_document()))
    declared = {f.name for f in dataclasses.fields(Document)}
    assert declared - serialized - excluded == set()


# ------------------------------------------------- what may be *stored*


def test_a_clean_document_is_storable() -> None:
    assert is_cacheable_result(_document())


def test_a_failed_page_load_is_not_stored() -> None:
    doc = _document()
    doc.error = "navigation timeout"
    assert not is_cacheable_result(doc)


def test_a_failed_llm_extraction_is_not_stored() -> None:
    """The defect this function exists for.

    A scrape with `extract` whose model call fails returns a *successful*
    document: markdown intact, `error` unset, `extract` null, `extract_error`
    populated. A write guarded only on `error` therefore stores that failure and
    serves it back for the whole TTL -- so one transient rate-limit becomes an hour
    of deterministically-empty extractions, with no further model calls attempted.
    """

    doc = _document()
    doc.extract = None
    doc.extract_error = "LLM request timed out"
    assert not is_cacheable_result(doc)


def test_a_degraded_but_successful_extraction_is_stored() -> None:
    """`extract_warning` means truncated input, not failure -- there *is* a result,
    and the warning travels with it."""

    doc = _document()
    doc.extract_error = None
    doc.extract_warning = "page truncated to fit the model's input budget"
    assert is_cacheable_result(doc)


# ------------------------------------------------- the cache must fail open


async def test_a_broken_backend_reads_as_a_miss() -> None:
    """A cache is an optimization; one that can turn a working scrape into a 500 is
    a liability. The concrete case: a deployment whose migrations have not run
    since `scrape_cache` was added, where every lookup raises `UndefinedTable`.
    """

    from agentpilot.jobs.cache import PostgresScrapeCache

    class _BrokenPool:
        def connection(self) -> object:
            raise RuntimeError('relation "scrape_cache" does not exist')

    cache = PostgresScrapeCache(_BrokenPool())
    assert await cache.get("k", max_age_ms=60_000) is None


async def test_a_broken_backend_swallows_the_write() -> None:
    """Failing to populate a cache costs the next caller a scrape. Raising costs
    this caller their response for no reason at all."""

    from agentpilot.jobs.cache import PostgresScrapeCache

    class _BrokenPool:
        def connection(self) -> object:
            raise RuntimeError("pool timeout")

    cache = PostgresScrapeCache(_BrokenPool())
    await cache.put("k", tenant="t", url="https://e.com", document=_document())
