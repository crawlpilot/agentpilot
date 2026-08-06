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
"""

from __future__ import annotations

import json
from typing import Any

from lxml.html import HtmlElement

from agentpilot.extraction import markdown_converter, postprocess, sanitizer, structured_data
from agentpilot.spi.actions import ExtractFormat


def extract(
    html: str,
    format: ExtractFormat = "markdown",
    main_content: bool = True,
    include_tags: tuple[str, ...] | None = None,
    exclude_tags: tuple[str, ...] | None = None,
    base_url: str | None = None,
    live_hydration: dict[str, Any] | None = None,
) -> str:
    if format == "html":
        return html

    if format == "structured_data":
        data = structured_data.extract_structured_data(html, base_url=base_url)
        if live_hydration:
            data["hydration"] = {**data["hydration"], **live_hydration}
        return json.dumps(data)

    result = _render(
        html,
        format=format,
        main_content=main_content,
        include_tags=include_tags,
        exclude_tags=exclude_tags,
        base_url=base_url,
    )

    if main_content and not result.strip():
        # Empty-content fallback: a boilerplate selector over-trimmed the
        # page down to nothing -- retry once against the full document
        # rather than returning empty-handed.
        result = _render(
            html,
            format=format,
            main_content=False,
            include_tags=include_tags,
            exclude_tags=exclude_tags,
            base_url=base_url,
        )

    return result


def _render(
    html: str,
    *,
    format: ExtractFormat,
    main_content: bool,
    include_tags: tuple[str, ...] | None,
    exclude_tags: tuple[str, ...] | None,
    base_url: str | None,
) -> str:
    root = sanitizer.sanitize(
        html,
        only_main_content=main_content,
        include_tags=include_tags,
        exclude_tags=exclude_tags,
        base_url=base_url,
    )
    if format == "markdown":
        return postprocess.postprocess(markdown_converter.to_markdown(root))
    return _extract_text(root)


def _extract_text(root: HtmlElement) -> str:
    text = root.text_content()
    return " ".join(text.split())
