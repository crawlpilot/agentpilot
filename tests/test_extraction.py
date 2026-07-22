"""Pure-transform unit tests for `baas.extraction` -- static HTML fixtures,
zero browser."""

from __future__ import annotations

from baas.extraction.extractor import extract

ARTICLE_HTML = """<html><body>
<header><nav>Home | About | Contact | Blog | Careers</nav></header>
<aside>Related links: foo, bar, baz</aside>
<article>
<h1>Main Headline</h1>
<p>This is the first paragraph of the real main content of the article, long
enough for trafilatura to consider it substantive body text rather than
boilerplate noise.</p>
<p>This is a second paragraph continuing the discussion with more unique
sentences that differ from the first paragraph entirely, to avoid any
near-duplicate detection heuristics kicking in unexpectedly.</p>
</article>
<footer>Copyright 2026 Example Corp. All rights reserved.</footer>
</body></html>"""


def test_markdown_extracts_main_content_only() -> None:
    text = extract(ARTICLE_HTML, format="markdown", main_content=True)
    assert "Main Headline" in text
    assert "first paragraph" in text
    assert "Careers" not in text
    assert "Copyright" not in text


def test_text_format_has_no_markdown_markup() -> None:
    text = extract(ARTICLE_HTML, format="text", main_content=True)
    assert "Main Headline" in text
    assert "# Main Headline" not in text


def test_html_format_is_raw_passthrough() -> None:
    text = extract(ARTICLE_HTML, format="html")
    assert text == ARTICLE_HTML
