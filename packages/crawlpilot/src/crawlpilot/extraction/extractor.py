"""Pure transform: HTML -> extracted content. No browser, no network.

Unit-testable against static HTML fixtures with zero browser -- this is a leaf
module (spi-only), imported by the driver to fulfill `ExtractAction`.

Ported from Firecrawl's HTML->Markdown pipeline: `sanitizer.py` (stage 1,
main-content extraction/selector-based sanitization) -> `markdown_converter.py`
(stage 2, HTML->Markdown) -> `postprocess.py` (stage 3, link/skip-link
cleanup) -> an empty-content fallback (stage 4, this module) that retries
without main-content filtering if the first pass produced nothing. `html`
format is unchanged: raw `page.content()` passthrough.

`structured_data` format (JSON-LD/meta/hydration state, see `structured_data
.py`) is a separate deterministic path that runs on a *fresh* parse of the raw
HTML, not `sanitizer.sanitize()`'s tree -- `sanitizer._drop_always()` strips
`head`/`meta`/`script` before this module would otherwise see them. It is
exempt from the empty-content main-content fallback below (that logic is
markdown/text-specific) and returns a JSON string, same as every other
format, to fit `spi.actions.ActionResult.extracts: list[str]`'s per-format
correlation.

`fit_markdown` inserts a stage 1b between sanitize and convert: `prune.py`
scores every element and drops the low-scoring subtrees the selector list in
`selectors.py` has no name for, and `relevance.py` additionally drops the
blocks that do not rank against `relevance_query` when one is given. It is a
*separate format* rather than a flag on `markdown` deliberately -- a caller can
request both and compare, which is the only honest way to tune a filter whose
whole job is throwing content away. `markdown` therefore always means "the
whole main content", and is never silently narrowed by a query.

`entities` (see `entities.py`) is deterministic regex extraction over the
unfiltered markdown -- unfiltered because an email address in a footer is
exactly the kind of thing both filters above are designed to discard, and a
caller asking for entities wants all of them.
"""

from __future__ import annotations

import json
from typing import Any

from lxml.html import HtmlElement

from crawlpilot.extraction import (
    entities,
    markdown_converter,
    postprocess,
    prune,
    relevance,
    sanitizer,
    structured_data,
)
from crawlpilot.spi.actions import ExtractFormat


def extract(
    html: str,
    format: ExtractFormat = "markdown",
    main_content: bool = True,
    include_tags: tuple[str, ...] | None = None,
    exclude_tags: tuple[str, ...] | None = None,
    base_url: str | None = None,
    live_hydration: dict[str, Any] | None = None,
    relevance_query: str | None = None,
    citations: bool = False,
) -> str:
    if format == "html":
        return html

    if format == "structured_data":
        data = structured_data.extract_structured_data(html, base_url=base_url)
        if live_hydration:
            data["hydration"] = {**data["hydration"], **live_hydration}
        return json.dumps(data)

    if format == "entities":
        markdown = _render(
            html,
            format="markdown",
            main_content=main_content,
            include_tags=include_tags,
            exclude_tags=exclude_tags,
            base_url=base_url,
        )
        return json.dumps(entities.extract_entities(markdown))

    # Attempts in decreasing strictness. Each stage of the pipeline can
    # legitimately reduce a page to nothing -- a boilerplate selector that
    # matched the whole body, a density score that fell below threshold
    # everywhere, a query nothing ranked against -- and returning "" for any of
    # them is the one unacceptable outcome: the caller cannot tell it from a
    # page that never loaded. So each filter is given up in turn, most
    # aggressive first, and the first attempt with content wins.
    for attempt_format, attempt_main_content in _fallback_chain(format, main_content):
        result = _render(
            html,
            format=attempt_format,
            main_content=attempt_main_content,
            include_tags=include_tags,
            exclude_tags=exclude_tags,
            base_url=base_url,
            relevance_query=relevance_query,
            citations=citations,
        )
        if result.strip():
            return result
    return result


def _fallback_chain(
    format: ExtractFormat, main_content: bool
) -> list[tuple[ExtractFormat, bool]]:
    """`(format, main_content)` pairs to try in order.

    For `fit_markdown` the scoring filters are dropped before main-content
    extraction is, because they are the newer and more aggressive of the two: a
    page that survives the selector pass and then scores badly everywhere is far
    more likely to be mis-scored than to be entirely chrome.
    """

    chain: list[tuple[ExtractFormat, bool]] = [(format, main_content)]
    if format == "fit_markdown":
        chain.append(("markdown", main_content))
    if main_content:
        chain.append((format, False))
        if format == "fit_markdown":
            chain.append(("markdown", False))
    return chain


def _render(
    html: str,
    *,
    format: ExtractFormat,
    main_content: bool,
    include_tags: tuple[str, ...] | None,
    exclude_tags: tuple[str, ...] | None,
    base_url: str | None,
    relevance_query: str | None = None,
    citations: bool = False,
) -> str:
    root = sanitizer.sanitize(
        html,
        only_main_content=main_content,
        include_tags=include_tags,
        exclude_tags=exclude_tags,
        base_url=base_url,
    )
    if format == "fit_markdown":
        root = prune.prune(root)
        if relevance_query:
            root = relevance.filter_by_query(root, relevance_query)
    if format in ("markdown", "fit_markdown"):
        return postprocess.postprocess(
            markdown_converter.to_markdown(root), citations=citations
        )
    return _extract_text(root)


def _extract_text(root: HtmlElement) -> str:
    text = root.text_content()
    return " ".join(text.split())
