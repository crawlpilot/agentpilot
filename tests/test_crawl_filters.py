"""`agentpilot.crawl.filters.evaluate` -- structural port of Firecrawl's
`WebCrawler.filterLinks`."""

from __future__ import annotations

from urllib.robotparser import RobotFileParser

from agentpilot.crawl.filters import FilterPolicy, evaluate

SEED = "https://example.com/blog/"


def test_same_host_is_always_allowed() -> None:
    decision = evaluate("https://example.com/blog/post-1", seed_url=SEED, policy=FilterPolicy())
    assert decision.allowed is True


def test_denied_file_extension_is_rejected() -> None:
    decision = evaluate(
        "https://example.com/blog/image.png", seed_url=SEED, policy=FilterPolicy()
    )
    assert decision.allowed is False
    assert decision.reason == "denied file extension"


def test_deny_files_false_allows_denied_extensions() -> None:
    policy = FilterPolicy(deny_files=False)
    decision = evaluate("https://example.com/blog/image.png", seed_url=SEED, policy=policy)
    assert decision.allowed is True


def test_subdomain_rejected_by_default() -> None:
    decision = evaluate(
        "https://blog.example.com/post", seed_url=SEED, policy=FilterPolicy()
    )
    assert decision.allowed is False
    assert "subdomain" in (decision.reason or "")


def test_subdomain_allowed_with_allow_subdomains() -> None:
    policy = FilterPolicy(allow_subdomains=True)
    decision = evaluate("https://blog.example.com/post", seed_url=SEED, policy=policy)
    assert decision.allowed is True


def test_external_domain_rejected_by_default() -> None:
    decision = evaluate("https://other.com/post", seed_url=SEED, policy=FilterPolicy())
    assert decision.allowed is False
    assert "external" in (decision.reason or "")


def test_external_domain_allowed_with_allow_external_links() -> None:
    policy = FilterPolicy(allow_external_links=True)
    decision = evaluate("https://other.com/post", seed_url=SEED, policy=policy)
    assert decision.allowed is True


def test_backward_crawling_rejected_by_default() -> None:
    # SEED is /blog/ -- /about is not below that path prefix.
    policy = FilterPolicy(allow_backward_crawling=False)
    decision = evaluate("https://example.com/about", seed_url=SEED, policy=policy)
    assert decision.allowed is False
    assert "backward" in (decision.reason or "")


def test_backward_crawling_allowed_when_flag_set() -> None:
    policy = FilterPolicy(allow_backward_crawling=True)
    decision = evaluate("https://example.com/about", seed_url=SEED, policy=policy)
    assert decision.allowed is True


def test_forward_path_never_counts_as_backward() -> None:
    policy = FilterPolicy(allow_backward_crawling=False)
    decision = evaluate("https://example.com/blog/post-1", seed_url=SEED, policy=policy)
    assert decision.allowed is True


def test_include_paths_regex_must_match() -> None:
    policy = FilterPolicy(include_paths=(r"^/blog/\d+$",))
    assert evaluate(
        "https://example.com/blog/123", seed_url=SEED, policy=policy
    ).allowed is True
    assert evaluate(
        "https://example.com/blog/post-1", seed_url=SEED, policy=policy
    ).allowed is False


def test_exclude_paths_regex_rejects_a_match() -> None:
    policy = FilterPolicy(exclude_paths=(r"^/blog/drafts/",))
    decision = evaluate("https://example.com/blog/drafts/1", seed_url=SEED, policy=policy)
    assert decision.allowed is False
    assert decision.reason == "matches exclude_paths"


def test_non_http_scheme_rejected() -> None:
    decision = evaluate("ftp://example.com/blog/x", seed_url=SEED, policy=FilterPolicy())
    assert decision.allowed is False


def test_robots_disallow_is_respected() -> None:
    parser = RobotFileParser()
    parser.parse(["User-agent: *", "Disallow: /blog/private"])
    decision = evaluate(
        "https://example.com/blog/private/x",
        seed_url=SEED,
        policy=FilterPolicy(),
        robots_parser=parser,
    )
    assert decision.allowed is False
    assert decision.reason == "disallowed by robots.txt"


def test_no_robots_parser_means_unrestricted() -> None:
    decision = evaluate(
        "https://example.com/blog/anything",
        seed_url=SEED,
        policy=FilterPolicy(),
        robots_parser=None,
    )
    assert decision.allowed is True
