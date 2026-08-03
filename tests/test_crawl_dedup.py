"""`agentpilot.crawl.dedup.normalize_url` -- the canonical-string producer
`crawl_tasks`'s `UNIQUE (job_id, url)` constraint relies on for dedup."""

from __future__ import annotations

from agentpilot.crawl.dedup import normalize_url


def test_passthrough_for_an_already_canonical_url() -> None:
    assert normalize_url("https://example.com/a") == "https://example.com/a"


def test_lowercases_scheme_and_host() -> None:
    assert normalize_url("HTTPS://Example.COM/a") == "https://example.com/a"


def test_strips_default_port() -> None:
    assert normalize_url("https://example.com:443/a") == "https://example.com/a"
    assert normalize_url("http://example.com:80/a") == "http://example.com/a"


def test_keeps_non_default_port() -> None:
    assert normalize_url("https://example.com:8443/a") == "https://example.com:8443/a"


def test_fragment_is_always_dropped() -> None:
    assert normalize_url("https://example.com/a#section") == "https://example.com/a"


def test_ignore_query_parameters_strips_the_query() -> None:
    result = normalize_url("https://example.com/a?utm=x&b=2", ignore_query_parameters=True)
    assert result == "https://example.com/a"


def test_query_kept_by_default() -> None:
    result = normalize_url("https://example.com/a?b=2")
    assert result == "https://example.com/a?b=2"


def test_deduplicate_similar_urls_strips_trailing_slash() -> None:
    assert normalize_url("https://example.com/a/", deduplicate_similar_urls=True) == (
        "https://example.com/a"
    )


def test_deduplicate_similar_urls_keeps_root_slash() -> None:
    assert normalize_url("https://example.com/", deduplicate_similar_urls=True) == (
        "https://example.com/"
    )


def test_deduplicate_similar_urls_sorts_query_params() -> None:
    a = normalize_url("https://example.com/a?b=2&a=1", deduplicate_similar_urls=True)
    b = normalize_url("https://example.com/a?a=1&b=2", deduplicate_similar_urls=True)
    assert a == b


def test_rejects_non_http_scheme() -> None:
    assert normalize_url("mailto:someone@example.com") is None
    assert normalize_url("javascript:void(0)") is None


def test_rejects_url_with_no_host() -> None:
    assert normalize_url("https:///a") is None
