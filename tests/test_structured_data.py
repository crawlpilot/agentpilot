"""Pure-transform unit tests for `agentpilot.extraction.structured_data` --
static HTML fixtures, zero browser."""

from __future__ import annotations

from agentpilot.extraction.structured_data import extract_structured_data

PLAIN_HTML = """<html><head><title>Plain Page</title></head>
<body><p>Nothing special here.</p></body></html>"""


def test_plain_page_returns_empty_json_ld_and_hydration() -> None:
    result = extract_structured_data(PLAIN_HTML)
    assert result["json_ld"] == []
    assert result["hydration"] == {}
    assert result["metadata"]["title"] == "Plain Page"


def test_valid_json_ld_block_is_parsed() -> None:
    html = """<html><head>
    <script type="application/ld+json">
    {"@context": "https://schema.org", "@type": "Product", "name": "Widget"}
    </script>
    </head><body></body></html>"""
    result = extract_structured_data(html)
    assert result["json_ld"] == [
        {"@context": "https://schema.org", "@type": "Product", "name": "Widget"}
    ]


def test_malformed_json_ld_block_is_skipped_not_fatal() -> None:
    html = """<html><head>
    <script type="application/ld+json">{not valid json,,,}</script>
    <script type="application/ld+json">{"@type": "Article", "headline": "Real"}</script>
    </head><body></body></html>"""
    result = extract_structured_data(html)
    assert result["json_ld"] == [{"@type": "Article", "headline": "Real"}]


def test_json_ld_array_and_graph_are_flattened() -> None:
    html = """<html><head>
    <script type="application/ld+json">[{"@type": "A"}, {"@type": "B"}]</script>
    <script type="application/ld+json">
    {"@context": "https://schema.org", "@graph": [{"@type": "C"}, {"@type": "D"}]}
    </script>
    </head><body></body></html>"""
    result = extract_structured_data(html)
    types = {item["@type"] for item in result["json_ld"]}
    assert types == {"A", "B", "C", "D"}


def test_og_and_twitter_and_dc_meta_extracted() -> None:
    html = """<html><head>
    <title>Fallback Title</title>
    <meta property="og:title" content="OG Title">
    <meta property="og:description" content="OG Desc">
    <meta name="twitter:card" content="summary_large_image">
    <meta name="dc.subject" content="Testing">
    <meta name="dcterms.created" content="2026-01-01">
    </head><body></body></html>"""
    result = extract_structured_data(html)
    meta = result["metadata"]
    assert meta["og:title"] == "OG Title"
    assert meta["og:description"] == "OG Desc"
    assert meta["twitter:card"] == "summary_large_image"
    assert meta["dc.subject"] == "Testing"
    assert meta["dcterms.created"] == "2026-01-01"
    assert meta["title"] == "Fallback Title"


def test_title_falls_back_to_og_title_when_title_tag_missing() -> None:
    html = """<html><head>
    <meta property="og:title" content="OG Fallback Title">
    </head><body></body></html>"""
    result = extract_structured_data(html)
    assert result["metadata"]["title"] == "OG Fallback Title"


def test_favicon_is_absolutified() -> None:
    html = """<html><head><link rel="icon" href="/favicon.ico"></head><body></body></html>"""
    result = extract_structured_data(html, base_url="https://example.com/dir/")
    assert result["metadata"]["favicon"] == "https://example.com/favicon.ico"


def test_generic_meta_pass_exposes_uncommon_tags() -> None:
    html = """<html><head><meta name="custom-flag" content="yes"></head><body></body></html>"""
    result = extract_structured_data(html)
    assert result["metadata"]["custom-flag"] == "yes"


def test_next_data_script_is_parsed() -> None:
    html = """<html><body>
    <script id="__NEXT_DATA__" type="application/json">
    {"props": {"pageProps": {"product": {"name": "Widget", "price": 9.99}}}}
    </script>
    </body></html>"""
    result = extract_structured_data(html)
    assert result["hydration"]["__NEXT_DATA__"]["props"]["pageProps"]["product"]["name"] == "Widget"


def test_nuxt_window_assignment_is_parsed_with_nested_braces() -> None:
    html = """<html><body>
    <script>window.__NUXT__ = {"data": [{"items": [{"id": 1, "meta": {"ok": true}}]}]};</script>
    </body></html>"""
    result = extract_structured_data(html)
    nuxt = result["hydration"]["__NUXT__"]
    assert nuxt["data"][0]["items"][0]["meta"]["ok"] is True


def test_multiple_hydration_globals_all_captured() -> None:
    html = """<html><body>
    <script id="__NEXT_DATA__" type="application/json">{"a": 1}</script>
    <script>window.__APOLLO_STATE__ = {"b": 2};</script>
    </body></html>"""
    result = extract_structured_data(html)
    assert result["hydration"]["__NEXT_DATA__"] == {"a": 1}
    assert result["hydration"]["__APOLLO_STATE__"] == {"b": 2}


def test_empty_html_returns_empty_shape() -> None:
    result = extract_structured_data("")
    assert result == {"metadata": {}, "json_ld": [], "hydration": {}}
