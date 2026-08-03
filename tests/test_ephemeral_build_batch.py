"""Pure unit test for `agentpilot.session.ephemeral._build_batch` -- no
browser, no network: confirms `ScrapeOptions.include_tags`/`exclude_tags`
actually reach the `ExtractAction`s it builds (the wiring gap the
Firecrawl-pipeline port closes)."""

from __future__ import annotations

from agentpilot.session.ephemeral import _build_batch, _effective_formats
from agentpilot.spi.actions import ExtractAction
from agentpilot.spi.scrape import ExtractConfig, ScrapeOptions


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


def test_effective_formats_appends_internal_markdown_when_extract_needs_it() -> None:
    options = ScrapeOptions(formats=("html",), extract=ExtractConfig(prompt="get the price"))
    assert _effective_formats(options) == ("html", "markdown")


def test_effective_formats_does_not_duplicate_markdown_already_requested() -> None:
    options = ScrapeOptions(formats=("markdown",), extract=ExtractConfig(prompt="get the price"))
    assert _effective_formats(options) == ("markdown",)


def test_effective_formats_unchanged_when_extract_not_set() -> None:
    options = ScrapeOptions(formats=("html", "text"))
    assert _effective_formats(options) == ("html", "text")


def test_build_batch_only_adds_one_extract_action_for_internal_markdown() -> None:
    options = ScrapeOptions(formats=("html",), extract=ExtractConfig(prompt="get the price"))
    batch = _build_batch("https://example.com", options)

    extract_actions = [a for a in batch if isinstance(a, ExtractAction)]
    assert [a.format for a in extract_actions] == ["html", "markdown"]
