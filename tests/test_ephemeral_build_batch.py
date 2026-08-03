"""Pure unit test for `agentpilot.session.ephemeral._build_batch` -- no
browser, no network: confirms `ScrapeOptions.include_tags`/`exclude_tags`
actually reach the `ExtractAction`s it builds (the wiring gap the
Firecrawl-pipeline port closes)."""

from __future__ import annotations

from agentpilot.session.ephemeral import _build_batch
from agentpilot.spi.actions import ExtractAction
from agentpilot.spi.scrape import ScrapeOptions


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
