"""Pure-transform unit tests for `agentpilot.extraction` -- static HTML fixtures,
zero browser."""

from __future__ import annotations

from agentpilot.extraction.extractor import extract
from agentpilot.extraction.postprocess import escape_link_label_newlines

ARTICLE_HTML = """<html><body>
<header><nav>Home | About | Contact | Blog | Careers</nav></header>
<aside>Related links: foo, bar, baz</aside>
<article>
<h1>Main Headline</h1>
<p>This is the first paragraph of the real main content of the article, long
enough for the extraction pipeline to consider it substantive body text
rather than boilerplate noise.</p>
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


def test_main_content_mode_preserves_links_and_images() -> None:
    html = "<article><p>See <a href='/x'>this</a> and <img src='/y.png' alt='z'></p></article>"
    md = extract(html, format="markdown", main_content=True)
    assert "[this](" in md
    assert "![z](" in md


def test_include_tags_keeps_only_matched_subtree() -> None:
    html = "<body><div class='ad'>buy now</div><article>real content here</article></body>"
    md = extract(html, format="markdown", main_content=False, include_tags=("article",))
    assert "real content" in md
    assert "buy now" not in md


def test_exclude_tags_removes_matched_selector() -> None:
    html = "<body><article>keep me</article><div class='promo'>drop me</div></body>"
    md = extract(html, format="markdown", main_content=False, exclude_tags=(".promo",))
    assert "keep me" in md
    assert "drop me" not in md


def test_main_content_removes_sidebar_and_cookie_banner() -> None:
    html = (
        "<body><article>content</article>"
        "<div class='sidebar'>related links</div>"
        "<div class='cookie'>we use cookies</div></body>"
    )
    md = extract(html, format="markdown", main_content=True)
    assert "content" in md
    assert "related links" not in md
    assert "we use cookies" not in md


def test_force_include_overrides_boilerplate_blocklist() -> None:
    html = "<body><div id='main' class='sidebar'>actually the real content</div></body>"
    md = extract(html, format="markdown", main_content=True)
    assert "actually the real content" in md


def test_table_renders_as_gfm_pipe_table() -> None:
    html = "<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>"
    md = extract(html, format="markdown", main_content=False)
    assert "| A | B |" in md
    assert "| --- | --- |" in md
    assert "| 1 | 2 |" in md


def test_code_block_preserves_language_and_fences_safely() -> None:
    html = "<pre><code class='language-python'>def f():\n    return 1</code></pre>"
    md = extract(html, format="markdown", main_content=False)
    assert "```python" in md
    assert "def f():" in md


def test_code_block_strips_line_number_gutter() -> None:
    html = "<pre><code><span class='line-numbers'>1</span>def f(): pass</code></pre>"
    md = extract(html, format="markdown", main_content=False)
    assert "1def f()" not in md


def test_skip_to_content_link_is_stripped() -> None:
    html = "<body><a href='#main'>Skip to Content</a><article>real</article></body>"
    md = extract(html, format="markdown")
    assert "Skip to Content" not in md
    assert "real" in md


def test_multiline_link_label_is_flattened() -> None:
    broken = "[Some\nlink text](https://example.com)"
    assert escape_link_label_newlines(broken) == "[Some\\\nlink text](https://example.com)"


def test_empty_main_content_falls_back_to_full_page() -> None:
    html = "<body><div class='sidebar'>only content on this page is here</div></body>"
    md = extract(html, format="markdown", main_content=True)
    assert "only content on this page is here" in md


def test_relative_links_and_images_are_absolutified() -> None:
    html = "<article><a href='/page'>link</a><img src='/img.png'></article>"
    md = extract(html, format="markdown", main_content=False, base_url="https://example.com/dir/")
    assert "https://example.com/page" in md
    assert "https://example.com/img.png" in md
