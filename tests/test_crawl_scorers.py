"""`agentpilot.crawl.scorers` and `agentpilot.crawl.nonsense` -- URL-only
judgement, no network.

These two decide what a bounded crawl comes back with. `limit: 500` against a
50,000-page site returns whatever the frontier ordered first, so the ranking is
not a nicety -- it is the answer.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agentpilot.crawl import nonsense, scorers

# ---------------------------------------------------------------- relevance


def test_relevance_is_the_fraction_of_query_terms_in_the_url() -> None:
    terms = scorers.tokenize("pricing enterprise")
    assert scorers.relevance("https://e.com/pricing/enterprise", terms) == 1.0
    assert scorers.relevance("https://e.com/pricing", terms) == 0.5
    assert scorers.relevance("https://e.com/blog/hello", terms) == 0.0


def test_relevance_with_no_query_is_neutral_not_zero() -> None:
    """Scoring everything 0 on this axis with no query would let `depth` alone
    decide the order -- a neutral 0.5 keeps the structural signals in proportion
    to each other."""

    assert scorers.relevance("https://e.com/anything", []) == 0.5


def test_relevance_reads_the_host_as_well_as_the_path() -> None:
    """Subdomain discovery is a Phase 3 source, so `docs.example.com/x` should
    rank for a docs query even though its path says nothing."""

    terms = scorers.tokenize("docs")
    assert scorers.relevance("https://docs.e.com/x", terms) == 1.0


# -------------------------------------------------------------------- depth


def test_depth_peaks_at_the_optimal_and_falls_off_both_ways() -> None:
    """Not "shallower is better": a site's root and top-level sections are mostly
    navigation, and the content lives two or three segments in."""

    scores = [scorers.depth(f"https://e.com{'/x' * n}") for n in range(6)]
    assert scores[2] == max(scores)
    assert scores[0] < scores[2]
    assert scores[5] < scores[2]


def test_depth_treats_the_root_as_depth_zero() -> None:
    assert scorers.depth("https://e.com/") == scorers.depth("https://e.com")


# -------------------------------------------------------------------- shape


def test_shape_prefers_content_paths_over_navigation() -> None:
    assert scorers.shape("https://e.com/blog/how-we-did-it") > scorers.shape(
        "https://e.com/tag/python"
    )


def test_shape_penalises_faceted_search_by_parameter_count() -> None:
    """Each parameter compounds: `?a=1` is often a real page, `?a=1&b=2&c=3` is one
    combination of a filter that generates thousands."""

    one = scorers.shape("https://e.com/products?color=red")
    three = scorers.shape("https://e.com/products?color=red&size=l&sort=price")
    assert three < one


def test_shape_penalises_numeric_trailing_segments() -> None:
    assert scorers.shape("https://e.com/blog/page/7") < scorers.shape(
        "https://e.com/blog/a-real-post"
    )


def test_shape_stays_in_range() -> None:
    urls = [
        "https://e.com/",
        "https://e.com/tag/x/category/y/page/2?a=1&b=2&c=3&d=4",
        "https://e.com/blog/products/docs/guide",
    ]
    for url in urls:
        assert 0.0 <= scorers.shape(url) <= 1.0


# ---------------------------------------------------------------- freshness


def test_freshness_decays_with_age() -> None:
    now = datetime(2026, 9, 26, tzinfo=UTC)
    fresh = scorers.freshness(now - timedelta(days=1), now=now)
    stale = scorers.freshness(now - timedelta(days=365), now=now)
    assert fresh > stale


def test_freshness_halves_at_the_half_life() -> None:
    now = datetime(2026, 9, 26, tzinfo=UTC)
    at_half_life = now - timedelta(days=scorers.FRESHNESS_HALF_LIFE_DAYS)
    assert scorers.freshness(at_half_life, now=now) == pytest.approx(0.5, abs=0.01)


def test_an_unknown_date_is_neutral_not_zero() -> None:
    """Most URLs have no date -- only feeds and sitemaps supply one. Scoring the
    rest at zero would rank every sitemap URL below every feed URL for a reason
    that is about the source, not the page."""

    assert scorers.freshness(None) == 0.5


def test_a_naive_timestamp_does_not_raise() -> None:
    """Feeds omit the offset often enough that this is a real input, and comparing
    a naive datetime against an aware `now()` raises."""

    assert 0.0 <= scorers.freshness(datetime(2026, 1, 1)) <= 1.0


def test_a_future_date_scores_top_rather_than_overflowing() -> None:
    now = datetime(2026, 9, 26, tzinfo=UTC)
    assert scorers.freshness(now + timedelta(days=30), now=now) == 1.0


# ---------------------------------------------------------------- composite


def test_composite_stays_in_range_whatever_the_url() -> None:
    urls = [
        "https://e.com/",
        "https://e.com/pricing",
        "https://e.com/tag/a/page/9?x=1&y=2&z=3",
        "https://sub.e.com/a/b/c/d/e/f",
    ]
    for url in urls:
        assert 0.0 <= scorers.composite(url) <= 1.0


def test_zeroed_weights_do_not_divide_by_zero() -> None:
    weights = scorers.ScoreWeights(relevance=0, depth=0, shape=0, freshness=0)
    assert scorers.composite("https://e.com/x", weights=weights) == 0.0


def test_a_weight_can_be_zeroed_without_rebalancing_the_rest() -> None:
    """`composite` normalizes by the weight total, so weights need not sum to 1 --
    which is what makes "turn freshness off" a one-field change."""

    weights = scorers.ScoreWeights(freshness=0)
    assert 0.0 <= scorers.composite("https://e.com/blog/x", weights=weights) <= 1.0


# -------------------------------------------------------------- rank_urls


def test_rank_urls_puts_the_query_match_first() -> None:
    """The property the deleted `rank.cosine_rank` tests asserted, now checked
    against the function that replaced it."""

    urls = ["https://x.test/about", "https://x.test/team", "https://x.test/pricing"]
    assert scorers.rank_urls(urls, query="pricing")[0] == "https://x.test/pricing"


def test_rank_urls_with_a_punctuation_only_query_keeps_input_order() -> None:
    """The other inherited case: a query that tokenizes to nothing must not
    reorder anything, since every URL then scores identically."""

    urls = ["https://x.test/a", "https://x.test/b"]
    assert scorers.rank_urls(urls, query="!!!") == urls


def test_rank_urls_is_stable_for_equal_scores() -> None:
    """A site whose URLs are structurally identical should come back in discovery
    order, not an arbitrary one."""

    urls = [f"https://e.com/blog/post-{n}" for n in range(10)]
    assert scorers.rank_urls(urls) == urls


def test_rank_urls_buries_navigation_beneath_content() -> None:
    """The whole point: in DOM order these arrive nav-first, because nav is the
    first thing in the markup on nearly every page."""

    urls = [
        "https://e.com/tag/python",
        "https://e.com/login",
        "https://e.com/terms",
        "https://e.com/blog/how-we-cut-latency",
        "https://e.com/docs/getting-started",
    ]
    ranked = scorers.rank_urls(urls)
    assert ranked[0] in ("https://e.com/blog/how-we-cut-latency", "https://e.com/docs/getting-started")
    assert ranked[-1] in ("https://e.com/tag/python", "https://e.com/login", "https://e.com/terms")


def test_rank_urls_uses_supplied_dates() -> None:
    now = datetime.now(UTC)
    urls = ["https://e.com/blog/old", "https://e.com/blog/new"]
    published = {
        "https://e.com/blog/old": now - timedelta(days=2000),
        "https://e.com/blog/new": now,
    }
    assert scorers.rank_urls(urls, published=published)[0] == "https://e.com/blog/new"


def test_rank_urls_on_an_empty_list() -> None:
    assert scorers.rank_urls([]) == []


# ----------------------------------------------------------------- nonsense


@pytest.mark.parametrize(
    "url",
    [
        "https://e.com/app.js",
        "https://e.com/styles/main.css",
        "https://e.com/fonts/inter.woff2",
        "https://e.com/favicon.ico",
        "https://e.com/robots.txt",
        "https://e.com/ads.txt",
        "https://e.com/_next/static/chunks/main.js",
        "https://e.com/sitemap_index.xml",
        "https://e.com/sitemaps/products-1.xml.gz",
        "https://e.com/.well-known/security.txt",
        "https://e.com/.git/config",
        "https://e.com/.env",
        "https://e.com/page%0Awith-newline",
    ],
)
def test_machinery_is_nonsense(url: str) -> None:
    assert nonsense.is_nonsense(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://e.com/",
        "https://e.com/pricing",
        "https://e.com/blog/2026/a-post",
        "https://e.com/products?color=red",
        "https://e.com/annual-report.pdf",
        "https://e.com/data/export.csv",
        "https://e.com/handbook.docx",
    ],
)
def test_pages_and_documents_are_not_nonsense(url: str) -> None:
    """PDFs, spreadsheets and CSVs pass *this* filter: they are content a caller
    may want, and once PDF extraction lands they are pages in every sense that
    matters.

    They do not currently survive discovery, though -- `filters._DENIED_EXTENSIONS`
    drops them independently, with its own "no pdf/document engine yet" note. This
    asserts what `nonsense` does, not what the pipeline returns."""

    assert not nonsense.is_nonsense(url)


def test_a_malformed_url_is_treated_as_nonsense() -> None:
    assert nonsense.is_nonsense("http://[::1")
