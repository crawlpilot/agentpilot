"""HTML in, useful content out: main-content extraction, markdown conversion,
deterministic structured data, and block/wall classification.

A base-install promise -- `pip install crawlpilot` with no extras gives you this
pipeline, which is why `lxml` and `cssselect` are ordinary dependencies rather
than an extra.

`extractor` is the entry point (HTML -> markdown/text/html), `structured_data`
the deterministic JSON-LD/OG/hydration reader, `entities` the deterministic
regex sweep, and `block_detect` the classification a consumer extends with site
knowledge -- `agentpilot.control.retail_extension` builds on its
`SiteChecker`/`Verdict`, which is what makes those four the published surface.
`markdown_converter`, `sanitizer`, `prune`, `relevance`, `postprocess` and
`selectors` are stages `extractor` composes.
"""

from __future__ import annotations

from crawlpilot.extraction import block_detect, entities, extractor, structured_data

__all__ = ["block_detect", "entities", "extractor", "structured_data"]
