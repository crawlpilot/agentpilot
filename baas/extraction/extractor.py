"""Pure transform: HTML -> extracted content. No browser, no network.

Unit-testable against static HTML fixtures with zero browser -- this is a leaf
module (spi-only), imported by the driver to fulfill `ExtractAction`.
"""

from __future__ import annotations

import trafilatura

from baas.spi.actions import ExtractFormat

_OUTPUT_FORMAT = {"markdown": "markdown", "text": "txt"}


def extract(html: str, format: ExtractFormat = "markdown", main_content: bool = True) -> str:
    if format == "html":
        return html

    result = trafilatura.extract(
        html,
        output_format=_OUTPUT_FORMAT[format],
        favor_precision=main_content,
        favor_recall=not main_content,
        include_comments=not main_content,
        include_tables=True,
        include_images=not main_content,
        include_links=not main_content,
    )
    return result or ""
