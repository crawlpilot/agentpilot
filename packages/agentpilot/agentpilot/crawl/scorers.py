"""Scoring a URL before fetching it, so a bounded crawl spends its budget well.

`frontier.expand_frontier` returns links in the order the parser found them, and
`worker_loop` enqueues them in that order. So a crawl with `limit: 500` against a
50,000-page site returns whichever 500 URLs happened to appear first in the DOM --
which, on a typical page, is the navigation, the footer, and the cookie policy.
The caller asked for 500 pages of a site and got its chrome.

Everything here is computed from the URL string alone. That is the constraint
that makes it useful: the decision has to be made *before* fetching, or it is not
a prioritization, it is a filter applied after paying the cost.

Ported from crawl4ai's `deep_crawling/scorers.py` (Apache-2.0; crawl4ai 0.9.4) --
the keyword-relevance, path-depth, content-type, freshness and composite scorers
are theirs. Two differences:

1. **Scores are 0..1, always.** Upstream's scorers return values on different and
   sometimes unbounded scales, then `CompositeScorer` sums them with weights --
   so one scorer's magnitude silently dominates the others regardless of its
   weight. Normalizing each one first is what makes a weight mean what it says.
2. **`freshness` reads a date it was given, not one it guessed from the URL.**
   Upstream parses years out of URL strings, which finds `/2019/03/post` and
   nothing else -- and mis-fires on `/product/model-2024`. Feeds and sitemaps
   both supply real dates (`<lastmod>`, `<updated>`); those are used when present
   and the URL is not mined for a substitute.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlparse

_WORD_RE = re.compile(r"[a-z0-9]+")

# Path segments that almost always lead away from content. Not a filter -- a
# caller may well want `/tag/python` -- just a reason to go there after the
# article pages.
_LOW_VALUE_SEGMENTS = frozenset(
    {
        "tag", "tags", "category", "categories", "author", "authors", "page",
        "archive", "archives", "search", "filter", "sort", "print", "amp",
        "feed", "rss", "login", "signin", "signup", "register", "cart",
        "checkout", "account", "privacy", "terms", "legal", "cookie",
        "cookies", "sitemap", "share",
    }
)  # fmt: skip

_CONTENT_SEGMENTS = frozenset(
    {
        "article", "articles", "blog", "post", "posts", "news", "story",
        "stories", "product", "products", "item", "docs", "doc", "guide",
        "guides", "tutorial", "reference", "help", "support", "faq", "pricing",
        "review", "reviews",
    }
)  # fmt: skip


@dataclass(frozen=True)
class ScoreWeights:
    """How much each signal counts. Need not sum to 1 -- `composite` normalizes by
    the total, so a caller can zero one out without rebalancing the rest."""

    relevance: float = 0.45
    """Query-keyword match. The largest share when a query exists, because it is
    the only signal that knows what the caller actually wants."""
    depth: float = 0.25
    shape: float = 0.20
    """Whether the path looks like a content page or like navigation."""
    freshness: float = 0.10
    """Smallest, and only meaningful for URLs that came with a date. Most do
    not."""


DEFAULT_WEIGHTS = ScoreWeights()

FRESHNESS_HALF_LIFE_DAYS = 180.0
"""A page six months old scores 0.5 on freshness, a year old 0.25. Chosen to be
gentle: recency matters for news and barely at all for documentation, and an
aggressive curve would bury a reference page that has not needed editing."""


def tokenize(text: str) -> list[str]:
    return _WORD_RE.findall(text.lower())


def relevance(url: str, query_terms: list[str]) -> float:
    """Fraction of the query's terms appearing in the URL's path and host.

    A blunt instrument by design -- the URL is all there is before fetching. It
    works because URLs are usually descriptive: a query for "pricing enterprise"
    finds `/pricing/enterprise` and not `/blog/2019/hello-world`.

    Returns 0.5, not 0.0, with no query: a neutral score, so `composite` without a
    query falls back to the structural signals rather than scoring everything
    zero on this axis and letting `depth` alone decide.
    """

    if not query_terms:
        return 0.5
    parsed = urlparse(url)
    haystack = set(tokenize(f"{parsed.hostname or ''} {parsed.path} {parsed.query}"))
    if not haystack:
        return 0.0
    matched = sum(1 for term in query_terms if term in haystack)
    return matched / len(query_terms)


def depth(url: str, *, optimal: int = 2) -> float:
    """Closeness to `optimal` path depth, falling off in both directions.

    Not "shallower is better". A site's homepage and its top-level sections are
    mostly navigation; the articles live two or three segments in. And an eight-
    segment URL is usually a faceted-search permutation. Both extremes deserve to
    lose to the middle.
    """

    segments = [part for part in (urlparse(url).path or "/").split("/") if part]
    distance = abs(len(segments) - optimal)
    return 1.0 / (1.0 + distance)


def shape(url: str) -> float:
    """Whether the path reads like content or like site furniture.

    Three things drag a score down, each for a reason visible in any crawl's
    results: a low-value segment (`/tag/`, `/page/`), a long query string
    (faceted search, which generates combinatorially many near-identical pages),
    and a numeric-only final segment (usually pagination).
    """

    parsed = urlparse(url)
    segments = [part.lower() for part in (parsed.path or "/").split("/") if part]

    score = 0.5
    if any(segment in _CONTENT_SEGMENTS for segment in segments):
        score += 0.3
    if any(segment in _LOW_VALUE_SEGMENTS for segment in segments):
        score -= 0.35
    if parsed.query:
        # Each parameter compounds: `?a=1` is often a real page, `?a=1&b=2&c=3` is
        # a filter combination of one.
        score -= min(0.1 * (parsed.query.count("&") + 1), 0.3)
    if segments and segments[-1].isdigit():
        score -= 0.15

    return max(0.0, min(1.0, score))


def freshness(published: datetime | None, *, now: datetime | None = None) -> float:
    """Exponential decay on age, or 0.5 when the date is unknown.

    Neutral rather than zero for undated URLs, which is most of them: scoring
    them at zero would rank every sitemap URL below every feed URL purely because
    feeds carry timestamps, which is an artifact of the source rather than a fact
    about the page.
    """

    if published is None:
        return 0.5
    reference = now or datetime.now(UTC)
    if published.tzinfo is None:
        published = published.replace(tzinfo=UTC)
    age_days = (reference - published).total_seconds() / 86_400
    if age_days <= 0:
        return 1.0
    return math.exp(-math.log(2) * age_days / FRESHNESS_HALF_LIFE_DAYS)


def composite(
    url: str,
    *,
    query_terms: list[str] | None = None,
    published: datetime | None = None,
    weights: ScoreWeights = DEFAULT_WEIGHTS,
    optimal_depth: int = 2,
    now: datetime | None = None,
) -> float:
    """One 0..1 score. Higher is fetched sooner."""

    total = weights.relevance + weights.depth + weights.shape + weights.freshness
    if total <= 0:
        return 0.0
    weighted = (
        weights.relevance * relevance(url, query_terms or [])
        + weights.depth * depth(url, optimal=optimal_depth)
        + weights.shape * shape(url)
        + weights.freshness * freshness(published, now=now)
    )
    return weighted / total


def rank_urls(
    urls: list[str],
    *,
    query: str | None = None,
    published: dict[str, datetime] | None = None,
    weights: ScoreWeights = DEFAULT_WEIGHTS,
    optimal_depth: int = 2,
) -> list[str]:
    """`urls` best-first. Stable: URLs scoring equally keep their input order, so
    a site whose URLs are all structurally identical comes back in the order
    discovery found them rather than in an arbitrary one."""

    query_terms = tokenize(query) if query else []
    dates = published or {}
    scored = [
        (
            composite(
                url,
                query_terms=query_terms,
                published=dates.get(url),
                weights=weights,
                optimal_depth=optimal_depth,
            ),
            index,
            url,
        )
        for index, url in enumerate(urls)
    ]
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [url for _, _, url in scored]
