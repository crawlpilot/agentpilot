"""Unit tests for the scoring/filtering stages -- `prune`, `relevance`,
`entities` and `postprocess.to_citations` -- plus their wiring through
`extractor.extract`. Static HTML fixtures, zero browser.

The div-soup fixture below is the whole reason these stages exist: it has no
semantic tag anywhere, so `sanitizer`'s selector list cannot see its navigation
or its share buttons, and every assertion about `fit_markdown` here fails
against the selector pass alone.
"""

from __future__ import annotations

import json

from lxml import html as lxml_html

from crawlpilot.extraction import prune, relevance, sanitizer
from crawlpilot.extraction.entities import extract_entities
from crawlpilot.extraction.extractor import extract
from crawlpilot.extraction.postprocess import to_citations

DIV_SOUP_HTML = """<html><body>
<div class="wrapper">
  <div class="flex gap-4">
    <a href="/"><span>Home</span></a>
    <a href="/pricing"><span>Pricing</span></a>
    <a href="/docs"><span>Docs</span></a>
    <a href="/blog"><span>Blog</span></a>
    <a href="/careers"><span>Careers</span></a>
  </div>
  <div class="flex flex-col">
    <div class="prose">Refunds are issued within fourteen days of the original
    purchase date, provided the item is returned unopened and in its original
    packaging. Shipping costs are not refunded.</div>
    <div class="prose">Warranty claims follow a different process entirely and
    are handled by the manufacturer. A warranty claim requires the original
    receipt and takes up to thirty days to resolve.</div>
  </div>
  <div class="social-share">
    <a href="https://twitter.com/x">Tweet</a>
    <a href="https://facebook.com/x">Share</a>
  </div>
</div>
</body></html>"""

SEMANTIC_HTML = """<html><body>
<header><nav>Home | About | Contact</nav></header>
<article>
<h1>Main Headline</h1>
<p>This is the first paragraph of the real main content of the article, long
enough for the extraction pipeline to consider it substantive body text.</p>
<p>Short one.</p>
<table><tr><th>Size</th><th>Price</th></tr><tr><td>S</td><td>$10</td></tr></table>
<pre><code class="language-python">x = 1</code></pre>
<ul><li>alpha</li><li>beta</li></ul>
</article>
<footer>Copyright 2026</footer>
</body></html>"""


def _pruned_text(html: str, **kwargs: object) -> str:
    root = sanitizer.sanitize(html, only_main_content=True)
    pruned = prune.prune(root, **kwargs)  # type: ignore[arg-type]
    return " ".join(fragment.strip() for fragment in pruned.itertext())


# --------------------------------------------------------------------- prune


def test_prune_drops_div_soup_navigation_the_selector_list_cannot_see() -> None:
    # The control: stage 1 alone keeps every one of these, because `.flex` and
    # `.social-share` are not on any boilerplate list.
    unpruned = extract(DIV_SOUP_HTML, format="markdown")
    assert "Careers" in unpruned
    assert "Tweet" in unpruned

    text = _pruned_text(DIV_SOUP_HTML)
    assert "Refunds are issued" in text
    assert "warranty claim" in text
    assert "Careers" not in text
    assert "Tweet" not in text


def test_prune_keeps_tables_code_blocks_and_headings() -> None:
    text = _pruned_text(SEMANTIC_HTML)
    assert "Main Headline" in text
    assert "substantive body text" in text
    # A data table is mostly markup by construction and a code block exists to
    # be left alone -- both are preserved without being scored.
    assert "Price" in text
    assert "x = 1" in text
    assert "alpha" in text


def test_prune_never_drops_the_root_even_when_everything_scores_badly() -> None:
    root = sanitizer.sanitize("<html><body><div><a href=/>x</a></div></body></html>",
                              only_main_content=False)
    assert prune.prune(root) is root


def test_prune_fixed_threshold_is_also_supported() -> None:
    text = _pruned_text(DIV_SOUP_HTML, threshold_type="fixed")
    assert "Refunds are issued" in text


def test_prune_min_word_threshold_drops_short_blocks() -> None:
    text = _pruned_text(SEMANTIC_HTML, min_word_threshold=8)
    assert "substantive body text" in text
    assert "Short one." not in text


def test_prune_honours_the_sanitizers_force_include_list() -> None:
    # `#main` is on `selectors.FORCE_INCLUDE_SELECTORS`; its content is a bare
    # link, which would otherwise score below any threshold.
    html = '<html><body><div id="main"><a href="/">go</a></div></body></html>'
    assert "go" in _pruned_text(html)


def test_prune_penalises_negative_class_names() -> None:
    # Deviation 1 from crawl4ai: upstream's `max(0, class_id_score)` makes this
    # metric dead code, so these two would score identically there.
    body = "Some reasonably long block of running text that sits in a div."
    scored = []
    for class_name in ("prose", "advert-banner"):
        root = lxml_html.fromstring(f'<div class="{class_name}">{body}</div>')
        metrics = prune._compute_metrics(root)
        scored.append(prune._composite_score(root, metrics[root], min_word_threshold=None))
    assert scored[0] > scored[1]


# ----------------------------------------------------------------- relevance


def _filtered_text(html: str, query: str, **kwargs: object) -> str:
    root = sanitizer.sanitize(html, only_main_content=True)
    filtered = relevance.filter_by_query(root, query, **kwargs)  # type: ignore[arg-type]
    return " ".join(fragment.strip() for fragment in filtered.itertext())


def test_relevance_keeps_the_block_that_answers_the_query() -> None:
    text = _filtered_text(DIV_SOUP_HTML, "warranty claim receipt")
    assert "warranty claim" in text
    assert "Refunds are issued" not in text


def test_relevance_fails_open_on_a_query_of_nothing_but_stopwords() -> None:
    # A filter that can empty a page must never do so quietly: the caller
    # cannot tell an over-filtered page from one that failed to load.
    text = _filtered_text(DIV_SOUP_HTML, "the and of it")
    assert "Refunds are issued" in text
    assert "warranty claim" in text


def test_relevance_fails_open_when_nothing_matches() -> None:
    text = _filtered_text(DIV_SOUP_HTML, "kubernetes sidecar telemetry")
    assert "Refunds are issued" in text


def test_relevance_fails_open_on_a_blank_query() -> None:
    assert "Refunds are issued" in _filtered_text(DIV_SOUP_HTML, "   ")


def test_relevance_keeps_headings_regardless_of_score() -> None:
    html = """<html><body><article>
    <h2>Shipping</h2>
    <p>Orders ship within one business day of payment clearing our processor.</p>
    <h2>Returns</h2>
    <p>A warranty claim requires the original receipt and takes thirty days.</p>
    </article></body></html>"""
    text = _filtered_text(html, "warranty claim receipt")
    assert "warranty claim" in text
    # Both headings survive: a heading dropped out from over its own kept
    # paragraph loses the reader the section the answer came from.
    assert "Returns" in text
    assert "Shipping" in text


# ------------------------------------------------------------------ entities


def test_entities_finds_the_obvious_kinds() -> None:
    text = (
        "Email support@example.com or call (555) 123-4567. "
        "Docs at https://example.com/docs. Price $1,299.00, now 15% off. "
        "Published 2026-09-25 at 14:30. Ticket "
        "f81d4fae-7dec-41d0-a765-00a0c91e6bf6."
    )
    found = extract_entities(text)
    assert found["email"] == ["support@example.com"]
    assert found["url"] == ["https://example.com/docs"]
    assert found["price"] == ["$1,299.00"]
    assert found["percentage"] == ["15%"]
    assert found["date_iso"] == ["2026-09-25"]
    assert found["time_24h"] == ["14:30"]
    assert found["uuid"] == ["f81d4fae-7dec-41d0-a765-00a0c91e6bf6"]
    assert "(555) 123-4567" in found["phone"]


def test_entities_does_not_read_a_handle_out_of_an_email_address() -> None:
    # crawl4ai's `@[\\w]{1,15}` matches `example` here, on every page that
    # lists an email address.
    found = extract_entities("Write to rahul@example.com about it.")
    assert "handle" not in found
    found = extract_entities("Follow @crawlpilot for updates.")
    assert found["handle"] == ["@crawlpilot"]


def test_entities_ipv4_is_octet_validated_and_run_bounded() -> None:
    assert extract_entities("Host 192.168.1.10 responded.")["ipv4"] == ["192.168.1.10"]
    # Out-of-range octets are not addresses.
    assert "ipv4" not in extract_entities("Build 999.1.1.1 failed.")
    # Nor is any four-group window inside a longer dotted run -- upstream's
    # `\b`-bounded pattern reads `1.2.3.4` out of `1.2.3.4.5`.
    assert "ipv4" not in extract_entities("Requires library 1.2.3.4.5 or newer.")
    # A bare four-group version string is genuinely ambiguous and does match;
    # no pattern can separate it from an address without the sentence around it.
    assert extract_entities("library 1.2.3.4")["ipv4"] == ["1.2.3.4"]


def test_entities_omits_the_noisy_patterns_upstream_ships() -> None:
    # `number` and `postal_us` (any five-digit run) fire on every price list;
    # `credit_card` and `iban` are not capabilities worth shipping.
    found = extract_entities("Order 48291 shipped; 3 items; total 1,204 units.")
    assert set(found) <= {"phone", "time_24h"}
    assert "number" not in found
    assert "postal_us" not in found
    assert "credit_card" not in found


def test_entities_deduplicates_and_preserves_first_appearance_order() -> None:
    found = extract_entities("b@x.com then a@x.com then b@x.com again")
    assert found["email"] == ["b@x.com", "a@x.com"]


def test_entities_on_empty_text() -> None:
    assert extract_entities("") == {}


# ----------------------------------------------------------------- citations


def test_citations_number_links_and_append_a_reference_block() -> None:
    out = to_citations("See [the docs](https://example.com/docs) for more.")
    assert "[the docs][1]" in out
    assert "## References" in out
    assert "[1]: https://example.com/docs" in out


def test_citations_collapse_a_repeated_url_onto_one_entry() -> None:
    out = to_citations("[a](https://e.com/x) and [b](https://e.com/x) and [c](https://e.com/y)")
    assert "[a][1]" in out and "[b][1]" in out and "[c][2]" in out
    assert out.count("[1]: https://e.com/x") == 1


def test_citations_leave_images_and_fragment_links_alone() -> None:
    out = to_citations("![logo](https://e.com/l.png) and [below](#pricing)")
    assert "![logo](https://e.com/l.png)" in out
    assert "[below](#pricing)" in out
    assert "## References" not in out


def test_citations_leave_fenced_code_alone() -> None:
    out = to_citations("Real [link](https://e.com/a).\n\n```\n[not](a-link)\n```\n")
    assert "[link][1]" in out
    assert "[not](a-link)" in out


def test_citations_strip_the_title_attribute_from_the_reference() -> None:
    out = to_citations('[x](https://e.com/a "A title")')
    assert "[x][1]" in out
    assert "[1]: https://e.com/a" in out


def test_citations_return_markdown_unchanged_when_there_is_nothing_to_cite() -> None:
    assert to_citations("Just prose.") == "Just prose."


# -------------------------------------------------------- extractor wiring


def test_fit_markdown_format_end_to_end() -> None:
    fit = extract(DIV_SOUP_HTML, format="fit_markdown")
    assert "Refunds are issued" in fit
    assert "Careers" not in fit


def test_relevance_query_narrows_fit_markdown_but_never_markdown() -> None:
    query = "warranty claim receipt"
    fit = extract(DIV_SOUP_HTML, format="fit_markdown", relevance_query=query)
    assert "warranty claim" in fit
    assert "Refunds are issued" not in fit

    # `markdown` always means the whole main content -- otherwise a caller who
    # wanted both the full page and a filtered view could not ask for it.
    full = extract(DIV_SOUP_HTML, format="markdown", relevance_query=query)
    assert "Refunds are issued" in full


def test_entities_format_returns_json() -> None:
    html = '<html><body><article><p>Mail us at hi@example.com today.</p></article></body></html>'
    found = json.loads(extract(html, format="entities"))
    assert found["email"] == ["hi@example.com"]


def test_fit_markdown_falls_back_rather_than_returning_empty() -> None:
    # Every block here is a bare link, so the density pass scores the page to
    # nothing. The caller still gets the page.
    nav_only = '<html><body><div class="menu"><a href="/a">A</a><a href="/b">B</a></div></body></html>'
    assert "A" in extract(nav_only, format="fit_markdown")


def test_citations_reach_the_extractor_for_both_markdown_formats() -> None:
    for fmt in ("markdown", "fit_markdown"):
        out = extract(SEMANTIC_HTML, format=fmt, citations=True, base_url="https://e.com")
        assert "## References" not in out  # no links in the main content at all
    with_link = '<html><body><article><p>See <a href="/docs">docs</a> for the full policy text.</p></article></body></html>'
    out = extract(with_link, format="markdown", citations=True, base_url="https://e.com")
    assert "[docs][1]" in out
    assert "[1]: https://e.com/docs" in out


def test_an_empty_page_is_still_empty() -> None:
    assert extract("<html><body></body></html>", format="fit_markdown") == ""
