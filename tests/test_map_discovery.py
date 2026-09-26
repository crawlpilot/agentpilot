"""`agentpilot.crawl.seed.discover_for_map` -- the richer discovery ported from
Firecrawl: robots.txt-declared sitemaps, the bounded recursive-crawl fallback,
`filter_by_path`, and `search` ranking. Driver-free, exercised over
`pytest_httpserver` like the existing sitemap tests.

The two `rank.cosine_rank` tests that used to live here moved to
`test_crawl_scorers.py` as ranking assertions against `scorers.rank_urls`, which
replaced that module -- same property (the URL matching the query comes first),
now checked against the function that actually runs."""

from __future__ import annotations

import pytest
from pytest_httpserver import HTTPServer

from agentpilot.crawl import seed
from agentpilot.crawl.types import MapOptions
from crawlpilot.spi.egress import EgressPolicy

POLICY = EgressPolicy()


def _urlset(*locs: str) -> str:
    body = "".join(f"<url><loc>{loc}</loc></url>" for loc in locs)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{body}</urlset>"
    )


# --- robots.txt-declared sitemaps ---


async def test_discovers_a_sitemap_declared_only_in_robots_txt(httpserver: HTTPServer) -> None:
    declared = httpserver.url_for("/custom-sitemap.xml")
    httpserver.expect_request("/robots.txt").respond_with_data(
        f"User-agent: *\nSitemap: {declared}\n", content_type="text/plain"
    )
    httpserver.expect_request("/custom-sitemap.xml").respond_with_data(
        _urlset(httpserver.url_for("/from-robots")), content_type="application/xml"
    )
    # The default /sitemap.xml is absent -- the URL is reachable *only* via the
    # robots.txt Sitemap: line, which the prior port ignored.
    httpserver.expect_request("/sitemap.xml").respond_with_data("nope", status=404)

    options = MapOptions(url=httpserver.url_for("/"), sitemap="only")
    urls = [link.url for link in await seed.discover_for_map(options, POLICY)]
    assert httpserver.url_for("/from-robots") in urls


# --- bounded recursive-crawl fallback ---


async def test_recursive_crawl_respects_max_discovery_depth(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/").respond_with_data(
        f'<a href="{httpserver.url_for("/a")}">a</a>', content_type="text/html"
    )
    httpserver.expect_request("/a").respond_with_data(
        f'<a href="{httpserver.url_for("/b")}">b</a>', content_type="text/html"
    )
    httpserver.expect_request("/b").respond_with_data(
        f'<a href="{httpserver.url_for("/c")}">c</a>', content_type="text/html"
    )
    httpserver.expect_request("/robots.txt").respond_with_data("nope", status=404)
    httpserver.expect_request("/sitemap.xml").respond_with_data("nope", status=404)

    options = MapOptions(url=httpserver.url_for("/"), sitemap="skip", max_discovery_depth=1)
    urls = [link.url for link in await seed.discover_for_map(options, POLICY)]
    assert httpserver.url_for("/a") in urls  # depth 0's links
    assert httpserver.url_for("/b") in urls  # depth 1's links
    assert httpserver.url_for("/c") not in urls  # depth 2 -- beyond the bound


async def test_recursive_crawl_stops_at_max_crawl_pages(httpserver: HTTPServer) -> None:
    # Seed links to 5 pages; with the page cap at 1 only the seed itself is
    # fetched, so its 5 links are the only ones discovered (none of them get
    # fetched to expand further).
    seed_links = "".join(
        f'<a href="{httpserver.url_for(f"/p{i}")}">p{i}</a>' for i in range(5)
    )
    httpserver.expect_request("/").respond_with_data(seed_links, content_type="text/html")
    for i in range(5):
        httpserver.expect_request(f"/p{i}").respond_with_data(
            f'<a href="{httpserver.url_for(f"/deep{i}")}">deep</a>', content_type="text/html"
        )
    httpserver.expect_request("/robots.txt").respond_with_data("nope", status=404)
    httpserver.expect_request("/sitemap.xml").respond_with_data("nope", status=404)

    options = MapOptions(
        url=httpserver.url_for("/"), sitemap="skip", max_discovery_depth=5
    )
    options.max_crawl_pages = 1
    urls = [link.url for link in await seed.discover_for_map(options, POLICY)]
    assert httpserver.url_for("/p0") in urls
    assert all(httpserver.url_for(f"/deep{i}") not in urls for i in range(5))


# --- filter_by_path ---


async def test_filter_by_path_restricts_results_to_the_seed_path(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/robots.txt").respond_with_data("nope", status=404)
    httpserver.expect_request("/sitemap.xml").respond_with_data(
        _urlset(httpserver.url_for("/docs/intro"), httpserver.url_for("/blog/post")),
        content_type="application/xml",
    )
    options = MapOptions(url=httpserver.url_for("/docs"), sitemap="only", filter_by_path=True)
    urls = [link.url for link in await seed.discover_for_map(options, POLICY)]
    assert httpserver.url_for("/docs/intro") in urls
    assert httpserver.url_for("/blog/post") not in urls


async def test_filter_by_path_disabled_keeps_off_path_links(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/robots.txt").respond_with_data("nope", status=404)
    httpserver.expect_request("/sitemap.xml").respond_with_data(
        _urlset(httpserver.url_for("/docs/intro"), httpserver.url_for("/blog/post")),
        content_type="application/xml",
    )
    options = MapOptions(url=httpserver.url_for("/docs"), sitemap="only", filter_by_path=False)
    urls = [link.url for link in await seed.discover_for_map(options, POLICY)]
    assert httpserver.url_for("/blog/post") in urls


# --- Phase 3: source selection, ranking, metadata, nonsense filtering ---


async def test_source_selection_narrows_what_runs(httpserver: HTTPServer) -> None:
    """`sources=["sitemap"]` must not also fetch the homepage. Discovery cost is
    the point of the parameter."""

    httpserver.expect_request("/sitemap.xml").respond_with_data(
        _urlset(httpserver.url_for("/from-sitemap")), content_type="application/xml"
    )
    options = MapOptions(url=httpserver.url_for("/"), sources=("sitemap",), limit=10)
    links = await seed.discover_for_map(options, POLICY)
    assert [link.url for link in links] == [httpserver.url_for("/from-sitemap")]


async def test_the_sitemap_flag_still_masks_the_source_list(
    httpserver: HTTPServer,
) -> None:
    """`sitemap="skip"` predates `sources` and callers use it, so it has to keep
    working as a mask over whatever sources are named."""

    httpserver.expect_request("/sitemap.xml").respond_with_data(
        _urlset(httpserver.url_for("/from-sitemap")), content_type="application/xml"
    )
    httpserver.expect_request("/").respond_with_data(
        f'<html><body><a href="{httpserver.url_for("/from-homepage")}">x</a></body></html>',
        content_type="text/html",
    )
    options = MapOptions(
        url=httpserver.url_for("/"),
        sources=("sitemap", "homepage"),
        sitemap="skip",
        limit=10,
        max_discovery_depth=0,
    )
    urls = [link.url for link in await seed.discover_for_map(options, POLICY)]
    assert httpserver.url_for("/from-homepage") in urls
    assert httpserver.url_for("/from-sitemap") not in urls


async def test_search_ranks_the_whole_set_not_an_arbitrary_slice(
    httpserver: HTTPServer,
) -> None:
    """The bug this fixes: the cap used to be applied during collection, so
    ranking only ever reordered the first `limit` URLs discovery happened to
    find. With `limit=2` the pricing page was simply never in the candidate set."""

    locs = [httpserver.url_for(f"/filler-{n}") for n in range(8)]
    locs.append(httpserver.url_for("/pricing"))
    httpserver.expect_request("/sitemap.xml").respond_with_data(
        _urlset(*locs), content_type="application/xml"
    )
    options = MapOptions(
        url=httpserver.url_for("/"),
        sources=("sitemap",),
        search="pricing",
        limit=2,
    )
    links = await seed.discover_for_map(options, POLICY)
    assert links[0].url == httpserver.url_for("/pricing")
    assert links[0].score is not None


async def test_no_search_leaves_the_score_unset(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/sitemap.xml").respond_with_data(
        _urlset(httpserver.url_for("/a")), content_type="application/xml"
    )
    options = MapOptions(url=httpserver.url_for("/"), sources=("sitemap",), limit=5)
    links = await seed.discover_for_map(options, POLICY)
    assert links[0].score is None


async def test_nonsense_is_filtered_out_of_the_results(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/sitemap.xml").respond_with_data(
        _urlset(
            httpserver.url_for("/real-page"),
            httpserver.url_for("/static/app.js"),
            httpserver.url_for("/favicon.ico"),
        ),
        content_type="application/xml",
    )
    options = MapOptions(url=httpserver.url_for("/"), sources=("sitemap",), limit=10)
    urls = [link.url for link in await seed.discover_for_map(options, POLICY)]
    assert urls == [httpserver.url_for("/real-page")]


async def test_nonsense_filtering_can_be_turned_off(httpserver: HTTPServer) -> None:
    """Uses a `.well-known` path rather than a `.js` one on purpose: assets are
    dropped by `filters.deny_files` regardless of this flag, so a test asserting
    `.js` comes back with `filter_nonsense=False` would be asserting something the
    pipeline never does. `/.well-known/...` has no extension, so `nonsense` is the
    only thing standing between it and the result set."""

    only_nonsense_catches_this = httpserver.url_for("/.well-known/change-password")
    httpserver.expect_request("/sitemap.xml").respond_with_data(
        _urlset(httpserver.url_for("/real-page"), only_nonsense_catches_this),
        content_type="application/xml",
    )

    on = MapOptions(url=httpserver.url_for("/"), sources=("sitemap",), limit=10)
    assert only_nonsense_catches_this not in [
        link.url for link in await seed.discover_for_map(on, POLICY)
    ]

    off = MapOptions(
        url=httpserver.url_for("/"), sources=("sitemap",), limit=10, filter_nonsense=False
    )
    assert only_nonsense_catches_this in [
        link.url for link in await seed.discover_for_map(off, POLICY)
    ]


async def test_include_metadata_fills_titles_and_drops_dead_urls(
    httpserver: HTTPServer,
) -> None:
    """The head fetch doubles as the liveness check -- a URL an index remembers and
    the site no longer serves is not a result."""

    httpserver.expect_request("/sitemap.xml").respond_with_data(
        _urlset(httpserver.url_for("/alive"), httpserver.url_for("/dead")),
        content_type="application/xml",
    )
    httpserver.expect_request("/alive").respond_with_data(
        "<html><head><title>Alive</title>"
        '<meta name="description" content="Still here"></head><body>x</body></html>',
        content_type="text/html",
    )
    httpserver.expect_request("/dead").respond_with_data("", status=404)

    options = MapOptions(
        url=httpserver.url_for("/"),
        sources=("sitemap",),
        limit=10,
        include_metadata=True,
        detect_soft_404=False,
    )
    links = await seed.discover_for_map(options, POLICY)
    assert [link.url for link in links] == [httpserver.url_for("/alive")]
    assert links[0].title == "Alive"
    assert links[0].description == "Still here"


# --- Phase 4: per-subdomain scanning ---


async def test_discovered_subdomains_are_scanned_for_their_own_urls(
    httpserver: HTTPServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What makes `crt`/`wayback` worth enabling. They report *hostnames*; without
    this pass a certificate covering `docs.example.com` widened the result set only
    through URLs those sources already happened to hold."""

    from agentpilot.crawl import probe, sources

    httpserver.expect_request("/sitemap.xml").respond_with_data(
        _urlset(httpserver.url_for("/apex-page")), content_type="application/xml"
    )

    # Stand in for DNS and for the per-host pass: both would otherwise reach the
    # real network, and what is under test is the orchestration between them.
    async def fake_resolve(candidates: set[str], **kwargs: object) -> set[str]:
        return {host for host in candidates if host.startswith(("docs.", "api."))}

    real_gather = sources.gather

    async def fake_gather(names: tuple[str, ...], ctx: sources.SourceContext):
        if ctx.base_domain.startswith(("docs.", "api.")):
            report = sources.SourceReport()
            report.outcomes["sitemap"] = sources.SourceOutcome(
                urls=[f"https://{ctx.base_domain}/from-subdomain"]
            )
            return report
        report = await real_gather(names, ctx)
        report.outcomes["crt"] = sources.SourceOutcome(
            hosts={"docs.localhost", "api.localhost", "dead.localhost"}
        )
        return report

    monkeypatch.setattr(probe, "resolve_hosts", fake_resolve)
    monkeypatch.setattr(sources, "gather", fake_gather)

    options = MapOptions(
        url=httpserver.url_for("/"),
        sources=("sitemap",),
        include_subdomains=True,
        limit=50,
        detect_soft_404=False,
        allow_external_links=True,
    )
    urls = [link.url for link in await seed.discover_for_map(options, POLICY)]

    assert "https://docs.localhost/from-subdomain" in urls
    assert "https://api.localhost/from-subdomain" in urls
    # `dead.localhost` did not resolve, so it was never scanned.
    assert not any("dead.localhost" in url for url in urls)


async def test_the_subdomain_scan_is_bounded(
    httpserver: HTTPServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each host costs a DNS lookup and two requests, so an unbounded pass turns one
    map into fifty."""

    from agentpilot.crawl import probe, sources

    httpserver.expect_request("/sitemap.xml").respond_with_data(
        _urlset(httpserver.url_for("/apex")), content_type="application/xml"
    )
    many = {f"h{n}.localhost" for n in range(30)}
    scanned: list[str] = []

    async def fake_resolve(candidates: set[str], **kwargs: object) -> set[str]:
        return set(candidates)

    real_gather = sources.gather

    async def fake_gather(names: tuple[str, ...], ctx: sources.SourceContext):
        if ctx.base_domain.endswith(".localhost"):
            scanned.append(ctx.base_domain)
            return sources.SourceReport()
        report = await real_gather(names, ctx)
        report.outcomes["crt"] = sources.SourceOutcome(hosts=many)
        return report

    monkeypatch.setattr(probe, "resolve_hosts", fake_resolve)
    monkeypatch.setattr(sources, "gather", fake_gather)

    options = MapOptions(
        url=httpserver.url_for("/"),
        sources=("sitemap",),
        include_subdomains=True,
        max_subdomains=4,
        limit=50,
        detect_soft_404=False,
    )
    await seed.discover_for_map(options, POLICY)
    assert len(scanned) == 4
    # Sorted selection, so which four is deterministic rather than set-order luck.
    assert scanned == sorted(scanned)


async def test_no_subdomain_scan_without_include_subdomains(
    httpserver: HTTPServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentpilot.crawl import probe, sources

    httpserver.expect_request("/sitemap.xml").respond_with_data(
        _urlset(httpserver.url_for("/apex")), content_type="application/xml"
    )
    resolved_called = False

    async def fake_resolve(candidates: set[str], **kwargs: object) -> set[str]:
        nonlocal resolved_called
        resolved_called = True
        return set()

    real_gather = sources.gather

    async def fake_gather(names: tuple[str, ...], ctx: sources.SourceContext):
        report = await real_gather(names, ctx)
        report.outcomes["crt"] = sources.SourceOutcome(hosts={"docs.localhost"})
        return report

    monkeypatch.setattr(probe, "resolve_hosts", fake_resolve)
    monkeypatch.setattr(sources, "gather", fake_gather)

    options = MapOptions(
        url=httpserver.url_for("/"),
        sources=("sitemap",),
        include_subdomains=False,
        limit=50,
        detect_soft_404=False,
    )
    await seed.discover_for_map(options, POLICY)
    assert not resolved_called
