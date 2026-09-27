"""Pure unit test for `crawlpilot.session.ephemeral._build_batch` -- no
browser, no network: confirms `ScrapeOptions.include_tags`/`exclude_tags`
actually reach the `ExtractAction`s it builds (the wiring gap the
Firecrawl-pipeline port closes)."""

from __future__ import annotations

from crawlpilot.session.ephemeral import (
    _build_batch,
    _effective_formats,
    _search_engine_referer,
)
from crawlpilot.spi.actions import ExtractAction, NavigateAction
from crawlpilot.spi.scrape import ExtractConfig, ScrapeOptions


def test_build_batch_threads_include_and_exclude_tags_into_extract_actions() -> None:
    options = ScrapeOptions(
        formats=("markdown", "text"),
        include_tags=("article",),
        exclude_tags=(".promo",),
    )
    batch = _build_batch("https://example.com", options)

    extract_actions = [a for a in batch if isinstance(a, ExtractAction)]
    assert len(extract_actions) == 2
    for action in extract_actions:
        assert action.include_tags == ("article",)
        assert action.exclude_tags == (".promo",)


def test_effective_formats_appends_the_internal_formats_extract_needs() -> None:
    """Both renderings, not just `markdown`.

    The extractor picks between them by size: a page whose full markdown overflows
    the model's input budget extracts far better from the pruned rendering than
    from the first 40k characters of the raw one, because truncation cuts the end
    of the document while pruning cuts the navigation. Deriving the second costs
    one pass over HTML already in memory.
    """

    options = ScrapeOptions(formats=("html",), extract=ExtractConfig(prompt="get the price"))
    assert _effective_formats(options) == ("html", "markdown", "fit_markdown")


def test_effective_formats_does_not_duplicate_a_format_already_requested() -> None:
    options = ScrapeOptions(formats=("markdown",), extract=ExtractConfig(prompt="get the price"))
    assert _effective_formats(options) == ("markdown", "fit_markdown")

    both = ScrapeOptions(
        formats=("markdown", "fit_markdown"), extract=ExtractConfig(prompt="x")
    )
    assert _effective_formats(both) == ("markdown", "fit_markdown")


def test_effective_formats_preserves_the_callers_order() -> None:
    """`extracts` is index-correlated with this tuple, so reordering it would
    silently mismatch every format against the wrong content."""

    options = ScrapeOptions(
        formats=("text", "html"), extract=ExtractConfig(prompt="x")
    )
    assert _effective_formats(options)[:2] == ("text", "html")


def test_effective_formats_unchanged_when_extract_not_set() -> None:
    options = ScrapeOptions(formats=("html", "text"))
    assert _effective_formats(options) == ("html", "text")


def test_build_batch_adds_one_extract_action_per_effective_format() -> None:
    options = ScrapeOptions(formats=("html",), extract=ExtractConfig(prompt="get the price"))
    batch = _build_batch("https://example.com", options)

    extract_actions = [a for a in batch if isinstance(a, ExtractAction)]
    assert [a.format for a in extract_actions] == ["html", "markdown", "fit_markdown"]


def test_always_a_single_navigation_to_the_requested_url() -> None:
    # No homepage-first double navigation, with or without a referer -- one hit
    # straight to the product (Pulsar's visit() shape).
    for referer in (None, "https://www.google.com/"):
        batch = _build_batch(
            "https://www.walmart.com/ip/x/123", ScrapeOptions(), referer=referer
        )
        navs = [a for a in batch if isinstance(a, NavigateAction)]
        assert [n.url for n in navs] == ["https://www.walmart.com/ip/x/123"]


def test_referer_is_threaded_onto_the_navigation() -> None:
    batch = _build_batch(
        "https://www.walmart.com/ip/x/123",
        ScrapeOptions(),
        referer="https://www.google.com/",
    )
    nav = next(a for a in batch if isinstance(a, NavigateAction))
    assert nav.referer == "https://www.google.com/"


def test_no_referer_by_default() -> None:
    nav = next(
        a for a in _build_batch("https://x.test/p", ScrapeOptions())
        if isinstance(a, NavigateAction)
    )
    assert nav.referer is None


def test_search_engine_referer_only_for_hosted_urls() -> None:
    assert _search_engine_referer("https://www.walmart.com/ip/x/123") == "https://www.google.com/"
    assert _search_engine_referer("not-a-url") is None
