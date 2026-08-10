"""Relevance re-ranking for `/v1/map`'s `search` param -- port of
Firecrawl's `performCosineSimilarity` (`lib/map-cosine.ts`). A lightweight
lexical relevance sort (per-query-word frequency vectors, cosine similarity),
not embeddings: deliberately stdlib-only math so this stays inside the
driver-free, dependency-free `agentpilot.crawl` package. Ranks over the URL
string only, matching Firecrawl's V2 behavior (title/description are always
`None` here anyway, since map has no index/search source to populate them).
"""

from __future__ import annotations

import math
import re

from agentpilot.spi.crawl import MapLink

_WORD_SPLIT = re.compile(r"\W+")


def _text_to_vector(text: str, query_words: list[str]) -> list[float]:
    lowered = text.lower()
    length = len(lowered) or 1
    return [lowered.count(word) / length for word in query_words]


def _cosine(vec1: list[float], vec2: list[float]) -> float:
    dot = sum(a * b for a, b in zip(vec1, vec2, strict=True))
    mag1 = math.sqrt(sum(a * a for a in vec1))
    mag2 = math.sqrt(sum(b * b for b in vec2))
    if mag1 == 0 or mag2 == 0:
        return 0.0
    return dot / (mag1 * mag2)


def cosine_rank(links: list[MapLink], search: str) -> list[MapLink]:
    """Return `links` reordered most-relevant-first for `search`. A stable
    sort, so equally-scored links keep their discovery order (sitemap/crawl
    order). Empty query words (e.g. `search` is all punctuation) leave the
    order unchanged."""

    query_words = [w for w in _WORD_SPLIT.split(search.lower()) if w]
    if not query_words:
        return links

    search_vector = _text_to_vector(search, query_words)
    scored = [
        (_cosine(_text_to_vector(link.url, query_words), search_vector), idx, link)
        for idx, link in enumerate(links)
    ]
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [link for _, _, link in scored]
