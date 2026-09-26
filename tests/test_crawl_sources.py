"""The Phase 3 discovery sources and their composer, over `pytest_httpserver`.

Each source is driven against a real local HTTP server rather than a mocked
client, because what these modules actually do is cope with badly-behaved
responses -- crt.sh answering with an HTML error page under a 200, the CDX API
returning URLs with encoded newlines in them, a guessed feed path serving the
site's 404 page as `application/xml`. A mock returns whatever the test author
imagined; a server returns bytes.

The external sources' real hosts are never contacted: each module's URL constant
is redirected at the fixture server.
"""

from __future__ import annotations

import json
import re

import pytest
from pytest_httpserver import HTTPServer

from agentpilot.crawl import cc, crt, feeds, probe, soft404, sources, wayback
from crawlpilot.spi.egress import EgressPolicy

POLICY = EgressPolicy()

_ANY_PATH = re.compile(r"^/.*$")
"""A catch-all URI matcher, for the tests that need a host answering the same way
for *every* path -- which is the single-page-app shape `soft404` exists to
detect. `expect_request("")` matches the empty path only, and answers 500."""


@pytest.fixture(autouse=True)
def _reset_cc_index() -> None:
    """The index id is cached per process, so without this the second test to run
    passes because the first one populated it."""

    cc.reset_index_cache()


# ------------------------------------------------------------- Common Crawl


async def test_cc_returns_urls_from_the_latest_index(
    httpserver: HTTPServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    httpserver.expect_request("/collinfo.json").respond_with_json(
        [{"id": "CC-MAIN-2026-33"}, {"id": "CC-MAIN-2026-22"}]
    )
    httpserver.expect_request("/CC-MAIN-2026-33-index").respond_with_data(
        "\n".join(
            json.dumps({"url": f"https://x.test/page-{n}"}) for n in range(3)
        )
    )
    monkeypatch.setattr(cc, "COLLINFO_URL", httpserver.url_for("/collinfo.json"))
    monkeypatch.setattr(
        cc, "INDEX_URL_TEMPLATE", httpserver.url_for("/{index_id}-index")
    )

    urls = await cc.fetch_urls("x.test", POLICY)
    assert urls == [f"https://x.test/page-{n}" for n in range(3)]


async def test_cc_skips_malformed_jsonl_lines_and_keeps_the_rest(
    httpserver: HTTPServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One truncated line in a 40,000-line response is not a reason to discard the
    other 39,999."""

    httpserver.expect_request("/collinfo.json").respond_with_json([{"id": "IDX"}])
    httpserver.expect_request("/IDX-index").respond_with_data(
        '{"url": "https://x.test/a"}\n{"url": broken\n{"url": "https://x.test/b"}\n'
    )
    monkeypatch.setattr(cc, "COLLINFO_URL", httpserver.url_for("/collinfo.json"))
    monkeypatch.setattr(cc, "INDEX_URL_TEMPLATE", httpserver.url_for("/{index_id}-index"))

    assert await cc.fetch_urls("x.test", POLICY) == [
        "https://x.test/a",
        "https://x.test/b",
    ]


async def test_cc_respects_max_urls(
    httpserver: HTTPServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    httpserver.expect_request("/collinfo.json").respond_with_json([{"id": "IDX"}])
    httpserver.expect_request("/IDX-index").respond_with_data(
        "\n".join(json.dumps({"url": f"https://x.test/{n}"}) for n in range(100))
    )
    monkeypatch.setattr(cc, "COLLINFO_URL", httpserver.url_for("/collinfo.json"))
    monkeypatch.setattr(cc, "INDEX_URL_TEMPLATE", httpserver.url_for("/{index_id}-index"))

    assert len(await cc.fetch_urls("x.test", POLICY, max_urls=5)) == 5


async def test_cc_fails_open_when_the_index_list_is_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cc, "COLLINFO_URL", "http://127.0.0.1:1/collinfo.json")
    assert await cc.fetch_urls("x.test", POLICY) == []


async def test_cc_treats_a_404_index_response_as_no_records(
    httpserver: HTTPServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The index answers 404 for a domain it has never seen. That is an answer, not
    a failure."""

    httpserver.expect_request("/collinfo.json").respond_with_json([{"id": "IDX"}])
    httpserver.expect_request("/IDX-index").respond_with_data("", status=404)
    monkeypatch.setattr(cc, "COLLINFO_URL", httpserver.url_for("/collinfo.json"))
    monkeypatch.setattr(cc, "INDEX_URL_TEMPLATE", httpserver.url_for("/{index_id}-index"))

    assert await cc.fetch_urls("x.test", POLICY) == []


# ----------------------------------------------------------------- Wayback


async def test_wayback_returns_in_domain_urls(
    httpserver: HTTPServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    httpserver.expect_request("/cdx").respond_with_data(
        "https://x.test/a\nhttps://blog.x.test/b\nhttps://other.test/c\n"
    )
    monkeypatch.setattr(wayback, "CDX_URL", httpserver.url_for("/cdx"))

    urls = await wayback.fetch_urls("x.test", POLICY)
    assert urls == ["https://x.test/a", "https://blog.x.test/b"]


async def test_wayback_excludes_subdomains_when_asked(
    httpserver: HTTPServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    httpserver.expect_request("/cdx").respond_with_data(
        "https://x.test/a\nhttps://blog.x.test/b\n"
    )
    monkeypatch.setattr(wayback, "CDX_URL", httpserver.url_for("/cdx"))

    urls = await wayback.fetch_urls("x.test", POLICY, include_subdomains=False)
    assert urls == ["https://x.test/a"]


async def test_wayback_reports_the_hosts_it_saw(
    httpserver: HTTPServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The archive knows about subdomains no certificate ever covered, so the same
    query doubles as a host source."""

    httpserver.expect_request("/cdx").respond_with_data(
        "https://x.test/a\nhttps://api.x.test/b\nhttps://shop.x.test/c\n"
    )
    monkeypatch.setattr(wayback, "CDX_URL", httpserver.url_for("/cdx"))

    assert await wayback.fetch_hosts("x.test", POLICY) == {
        "x.test",
        "api.x.test",
        "shop.x.test",
    }


async def test_wayback_fails_open_on_a_rate_limit(
    httpserver: HTTPServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    httpserver.expect_request("/cdx").respond_with_data("slow down", status=429)
    monkeypatch.setattr(wayback, "CDX_URL", httpserver.url_for("/cdx"))
    assert await wayback.fetch_urls("x.test", POLICY) == []


# ------------------------------------------------------- Certificate logs


async def test_crt_extracts_hosts_from_both_name_fields(
    httpserver: HTTPServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    httpserver.expect_request("/").respond_with_json(
        [
            {"common_name": "www.x.test", "name_value": "www.x.test\napi.x.test"},
            {"common_name": "*.eu.x.test", "name_value": "*.eu.x.test"},
            {"common_name": "unrelated.test", "name_value": "unrelated.test"},
        ]
    )
    monkeypatch.setattr(crt, "CRT_SH_URL", httpserver.url_for("/"))

    hosts = await crt.fetch_hosts("x.test", POLICY)
    assert hosts == {"www.x.test", "api.x.test", "eu.x.test"}


async def test_crt_survives_an_html_error_page_served_with_a_200(
    httpserver: HTTPServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """crt.sh does exactly this under load, which is why the JSON decode is
    guarded rather than trusted."""

    httpserver.expect_request("/").respond_with_data(
        "<html><body>Service temporarily unavailable</body></html>",
        content_type="text/html",
    )
    monkeypatch.setattr(crt, "CRT_SH_URL", httpserver.url_for("/"))
    assert await crt.fetch_hosts("x.test", POLICY) == set()


async def test_crt_caps_the_host_count(
    httpserver: HTTPServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    httpserver.expect_request("/").respond_with_json(
        [{"common_name": f"h{n}.x.test", "name_value": f"h{n}.x.test"} for n in range(50)]
    )
    monkeypatch.setattr(crt, "CRT_SH_URL", httpserver.url_for("/"))
    assert len(await crt.fetch_hosts("x.test", POLICY, max_hosts=10)) <= 10


# -------------------------------------------------------------------- feeds


def test_feed_parses_atom_entries_with_dates() -> None:
    xml = """<?xml version="1.0"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <title>First</title>
        <link rel="alternate" href="https://x.test/first"/>
        <updated>2026-09-01T10:00:00Z</updated>
      </entry>
    </feed>"""
    entries = feeds.parse(xml, base_url="https://x.test/feed")
    assert len(entries) == 1
    assert entries[0].url == "https://x.test/first"
    assert entries[0].title == "First"
    assert entries[0].published is not None
    assert entries[0].published.year == 2026


def test_feed_parses_rss_with_an_rfc_2822_date() -> None:
    xml = """<?xml version="1.0"?>
    <rss version="2.0"><channel>
      <item>
        <title>Post</title>
        <link>https://x.test/post</link>
        <pubDate>Tue, 01 Sep 2026 10:00:00 GMT</pubDate>
      </item>
    </channel></rss>"""
    entries = feeds.parse(xml, base_url="https://x.test/rss")
    assert entries[0].url == "https://x.test/post"
    assert entries[0].published is not None


def test_feed_prefers_the_alternate_link_over_an_enclosure() -> None:
    """Taking the first `<link>` indiscriminately returns podcast MP3s instead of
    episode pages."""

    xml = """<?xml version="1.0"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <link rel="enclosure" href="https://x.test/ep1.mp3"/>
        <link rel="alternate" href="https://x.test/ep1"/>
      </entry>
    </feed>"""
    assert feeds.parse(xml, base_url="https://x.test/f")[0].url == "https://x.test/ep1"


def test_feed_rejects_html_served_at_a_guessed_path() -> None:
    """This is how a conventional-path guess against a site with no feed gets
    rejected: the 404 page does not parse as a feed."""

    assert feeds.parse("<html><body>Not found</body></html>", base_url="https://x.test/feed") == []


def test_feed_handles_a_naive_timestamp() -> None:
    xml = """<?xml version="1.0"?>
    <rss version="2.0"><channel><item>
      <link>https://x.test/a</link><pubDate>2026-09-01T10:00:00</pubDate>
    </item></channel></rss>"""
    entry = feeds.parse(xml, base_url="https://x.test/rss")[0]
    assert entry.published is not None
    assert entry.published.tzinfo is not None


async def test_feed_discovery_uses_a_declared_url_first(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/custom/feed.xml").respond_with_data(
        '<?xml version="1.0"?><rss version="2.0"><channel><item>'
        "<link>https://x.test/declared</link></item></channel></rss>",
        content_type="application/rss+xml",
    )
    entries = await feeds.discover(
        httpserver.url_for("/").rstrip("/"),
        POLICY,
        declared_urls=(httpserver.url_for("/custom/feed.xml"),),
        max_feeds=1,
    )
    assert [entry.url for entry in entries] == ["https://x.test/declared"]


# ------------------------------------------------------------------ soft 404


async def test_soft_404_fingerprints_a_200_answering_host(httpserver: HTTPServer) -> None:
    httpserver.expect_request(_ANY_PATH).respond_with_data(
        "<html><title>Not found</title><body>No such page</body></html>",
        content_type="text/html",
    )
    print_url = httpserver.url_for("/").rstrip("/")
    fp = await soft404.fingerprint(print_url, POLICY)
    assert fp is not None
    assert fp.title == "Not found"


async def test_a_host_that_answers_404_honestly_gets_no_fingerprint(
    httpserver: HTTPServer,
) -> None:
    """Nothing to compare against later, so there is nothing to filter on -- and
    `is_soft_404` then passes everything through."""

    httpserver.expect_request(_ANY_PATH).respond_with_data(
        "gone", status=404
    )
    assert await soft404.fingerprint(httpserver.url_for("/").rstrip("/"), POLICY) is None


def test_is_soft_404_ignores_a_per_response_nonce() -> None:
    """The reason the hash is over a digit-stripped, whitespace-collapsed sample
    rather than raw bytes: a CSRF token or request id in the markup otherwise
    makes every not-found page hash differently and the check never fires."""

    first = b"<html><body>Not found <span id='req-12345'>x</span></body></html>"
    second = b"<html><body>Not found <span id='req-98765'>x</span></body></html>"
    fp = soft404.Soft404Fingerprint(
        status_code=200,
        title=None,
        body_hash=soft404._normalized_hash(first),
        final_path=None,
    )
    assert soft404.is_soft_404(
        status_code=200, body=second, final_path=None, fingerprint=fp
    )


def test_is_soft_404_matches_on_the_redirect_destination() -> None:
    fp = soft404.Soft404Fingerprint(
        status_code=200, title=None, body_hash="unrelated", final_path="/404"
    )
    assert soft404.is_soft_404(
        status_code=200, body=b"anything", final_path="/404", fingerprint=fp
    )


def test_is_soft_404_does_not_match_on_an_empty_title() -> None:
    """Two pages sharing a blank title says nothing at all."""

    fp = soft404.Soft404Fingerprint(
        status_code=200, title=None, body_hash="unrelated", final_path=None
    )
    assert not soft404.is_soft_404(
        status_code=200, body=b"<html><body>Real page</body></html>",
        final_path=None, fingerprint=fp,
    )


def test_no_fingerprint_means_nothing_is_filtered() -> None:
    assert not soft404.is_soft_404(
        status_code=200, body=b"x", final_path=None, fingerprint=None
    )


# -------------------------------------------------------------------- probe


async def test_probe_keeps_paths_that_answer_and_drops_the_rest(
    httpserver: HTTPServer,
) -> None:
    httpserver.expect_request("/about").respond_with_data(
        "<html><body>About us</body></html>", content_type="text/html"
    )
    httpserver.expect_request("/pricing").respond_with_data("", status=404)

    found = await probe.probe_paths(
        httpserver.url_for("/").rstrip("/"), POLICY, paths=("/about", "/pricing")
    )
    assert found == [httpserver.url_for("/about")]


async def test_probe_without_a_fingerprint_trusts_every_200(
    httpserver: HTTPServer,
) -> None:
    """The control for the test below: this is the SPA failure mode -- the probe's
    output becomes a copy of its input, which is worse than nothing because it
    looks like data."""

    httpserver.expect_request(_ANY_PATH).respond_with_data(
        "<html><title>App</title><body>shell</body></html>", content_type="text/html"
    )
    found = await probe.probe_paths(
        httpserver.url_for("/").rstrip("/"), POLICY, paths=("/about", "/pricing", "/docs")
    )
    assert len(found) == 3


async def test_probe_with_a_fingerprint_drops_the_spa_shell(
    httpserver: HTTPServer,
) -> None:
    httpserver.expect_request(_ANY_PATH).respond_with_data(
        "<html><title>App</title><body>shell</body></html>", content_type="text/html"
    )
    origin = httpserver.url_for("/").rstrip("/")
    fp = await soft404.fingerprint(origin, POLICY)
    assert fp is not None

    found = await probe.probe_paths(
        origin, POLICY, paths=("/about", "/pricing", "/docs"), fingerprint=fp
    )
    assert found == []


async def test_resolve_hosts_keeps_only_what_dns_answers() -> None:
    """`localhost` resolves everywhere; the random name does not. This is the step
    that separates a certificate log's wishful thinking from hosts that exist."""

    resolved = await probe.resolve_hosts(
        {"localhost", "certainly-not-a-real-host-ab12cd34.invalid"}
    )
    assert resolved == {"localhost"}


async def test_resolve_hosts_on_an_empty_set() -> None:
    assert await probe.resolve_hosts(set()) == set()


def test_subdomain_candidates_are_built_from_the_prefix_list() -> None:
    candidates = probe.subdomain_candidates("x.test", extra=("weird",))
    assert "www.x.test" in candidates
    assert "api.x.test" in candidates
    assert "weird.x.test" in candidates


# ----------------------------------------------------------------- composer


async def test_gather_records_a_per_source_account(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/robots.txt").respond_with_data("", status=404)
    httpserver.expect_request("/sitemap.xml").respond_with_data(
        '<?xml version="1.0"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        "<url><loc>https://x.test/a</loc></url></urlset>",
        content_type="application/xml",
    )
    origin = httpserver.url_for("/").rstrip("/")
    report = await sources.gather(
        ("sitemap", "robots"),
        sources.SourceContext(
            base_domain="x.test", origin=origin, seed_url=origin, policy=POLICY
        ),
    )
    assert report.outcomes["sitemap"].urls == ["https://x.test/a"]
    assert report.outcomes["robots"].urls == []
    assert report.summary() == {"sitemap": "1", "robots": "0"}


async def test_a_failing_source_never_fails_the_gather(httpserver: HTTPServer) -> None:
    """Discovery from the working sources is a good answer; a 500 because one
    index was rate-limited is not."""

    httpserver.expect_request("/sitemap.xml").respond_with_data(
        '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        "<url><loc>https://x.test/a</loc></url></urlset>",
        content_type="application/xml",
    )
    origin = httpserver.url_for("/").rstrip("/")

    async def boom(ctx: sources.SourceContext) -> sources.SourceOutcome:
        raise RuntimeError("index unavailable")

    original = sources._dispatch

    async def patched(name: str, ctx: sources.SourceContext) -> sources.SourceOutcome:
        if name == "cc":
            return await boom(ctx)
        return await original(name, ctx)  # type: ignore[arg-type]

    sources._dispatch = patched  # type: ignore[assignment]
    try:
        report = await sources.gather(
            ("sitemap", "cc"),
            sources.SourceContext(
                base_domain="x.test", origin=origin, seed_url=origin, policy=POLICY
            ),
        )
    finally:
        sources._dispatch = original  # type: ignore[assignment]

    assert report.urls == ["https://x.test/a"]
    assert report.outcomes["cc"].error == "index unavailable"


async def test_a_slow_source_hits_its_own_deadline_only() -> None:
    """Per-source `wait_for`, not one timeout for the whole gather -- otherwise the
    slowest source sets the latency of the request."""

    async def slow(ctx: sources.SourceContext) -> sources.SourceOutcome:
        import asyncio

        await asyncio.sleep(5)
        return sources.SourceOutcome()

    original = sources._dispatch

    async def patched(name: str, ctx: sources.SourceContext) -> sources.SourceOutcome:
        return await slow(ctx)

    sources._dispatch = patched  # type: ignore[assignment]
    try:
        report = await sources.gather(
            ("cc",),
            sources.SourceContext(
                base_domain="x.test",
                origin="https://x.test",
                seed_url="https://x.test",
                policy=POLICY,
                source_timeout=0.05,
            ),
        )
    finally:
        sources._dispatch = original  # type: ignore[assignment]

    assert report.outcomes["cc"].error == "timeout"


async def test_the_homepage_hands_its_declared_feed_to_the_feed_source(
    httpserver: HTTPServer,
) -> None:
    """`homepage` runs before `feed` precisely so `feed` does not guess
    conventional paths at a site that already said where its feed is."""

    httpserver.expect_request("/").respond_with_data(
        '<html><head><link rel="alternate" type="application/rss+xml" '
        'href="/real/feed.xml"></head><body><a href="/a">a</a></body></html>',
        content_type="text/html",
    )
    httpserver.expect_request("/real/feed.xml").respond_with_data(
        '<?xml version="1.0"?><rss version="2.0"><channel><item>'
        "<link>https://x.test/from-declared-feed</link></item></channel></rss>",
        content_type="application/rss+xml",
    )
    origin = httpserver.url_for("/").rstrip("/")
    report = await sources.gather(
        ("homepage", "feed"),
        sources.SourceContext(
            base_domain="x.test", origin=origin, seed_url=f"{origin}/", policy=POLICY
        ),
    )
    assert "https://x.test/from-declared-feed" in report.outcomes["feed"].urls


async def test_gather_dedups_across_sources(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/robots.txt").respond_with_data(
        f"Sitemap: {httpserver.url_for('/sitemap.xml')}\n"
    )
    httpserver.expect_request("/sitemap.xml").respond_with_data(
        '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        "<url><loc>https://x.test/same</loc></url></urlset>",
        content_type="application/xml",
    )
    origin = httpserver.url_for("/").rstrip("/")
    report = await sources.gather(
        ("sitemap", "robots"),
        sources.SourceContext(
            base_domain="x.test", origin=origin, seed_url=origin, policy=POLICY
        ),
    )
    # Both sources found it; the merged view lists it once.
    assert report.urls == ["https://x.test/same"]


async def test_an_unknown_source_name_is_reported_not_raised() -> None:
    report = await sources.gather(
        ("nonsense",),  # type: ignore[arg-type]
        sources.SourceContext(
            base_domain="x.test",
            origin="https://x.test",
            seed_url="https://x.test",
            policy=POLICY,
        ),
    )
    assert report.outcomes["nonsense"].error is not None
