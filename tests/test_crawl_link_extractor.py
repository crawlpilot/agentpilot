"""`agentpilot.crawl.link_extractor.extract_links`."""

from __future__ import annotations

from agentpilot.crawl.link_extractor import extract_links

BASE = "https://example.com/blog/"


def test_resolves_relative_links_against_base() -> None:
    html = '<a href="post-1">One</a><a href="/about">About</a>'
    assert extract_links(html, BASE) == [
        "https://example.com/blog/post-1",
        "https://example.com/about",
    ]


def test_keeps_absolute_links_unchanged() -> None:
    html = '<a href="https://other.com/x">X</a>'
    assert extract_links(html, BASE) == ["https://other.com/x"]


def test_skips_non_navigable_hrefs() -> None:
    html = (
        '<a href="javascript:void(0)">JS</a>'
        '<a href="mailto:a@example.com">Mail</a>'
        '<a href="tel:+1234567890">Tel</a>'
        '<a href="#top">Fragment</a>'
        '<a href="">Empty</a>'
    )
    assert extract_links(html, BASE) == []


def test_ignores_non_anchor_tags() -> None:
    html = '<link href="/style.css"><img src="/pic.png"><a href="/real">Real</a>'
    assert extract_links(html, BASE) == ["https://example.com/real"]


def test_tolerates_malformed_html() -> None:
    html = '<div><a href="/a">A<a href="/b">B</div'
    assert extract_links(html, BASE) == [
        "https://example.com/a",
        "https://example.com/b",
    ]


def test_duplicate_hrefs_are_not_deduped_here() -> None:
    # Dedup is dedup.py's job, not the extractor's -- this just resolves
    # every <a href> it sees, in document order.
    html = '<a href="/a">A</a><a href="/a">A again</a>'
    assert extract_links(html, BASE) == ["https://example.com/a", "https://example.com/a"]
