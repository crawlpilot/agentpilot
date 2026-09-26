"""`crawlpilot.extraction.tables` -- the grid, and telling a data table from a
layout one. Static HTML, zero browser.

The span cases are the reason this module exists: pipe-markdown flattens
`colspan`/`rowspan` into ragged rows, and a caller wanting the data back has to
reconstruct a rectangle that the HTML already described.
"""

from __future__ import annotations

import json

from lxml import html as lxml_html

from crawlpilot.extraction.extractor import extract
from crawlpilot.extraction.tables import extract_table, extract_tables, is_data_table

SIZES_HTML = """<table summary="Sizes and prices">
  <caption>Available sizes</caption>
  <thead><tr><th>Size</th><th>EU</th><th>Price</th></tr></thead>
  <tbody>
    <tr><th rowspan="2">Small</th><td>36</td><td>$10</td></tr>
    <tr><td>38</td><td>$12</td></tr>
    <tr><th>Large</th><td colspan="2">Out of stock</td></tr>
  </tbody>
</table>"""


def _one(html: str) -> object:
    return extract_table(lxml_html.fromstring(html))


# -------------------------------------------------------------------- grid


def test_headers_caption_and_summary_are_all_captured() -> None:
    """All three are dropped by the markdown rendering, and all three are the
    labels that make the rows mean anything."""

    table = _one(SIZES_HTML)
    assert table.headers == ["Size", "EU", "Price"]  # type: ignore[attr-defined]
    assert table.caption == "Available sizes"  # type: ignore[attr-defined]
    assert table.summary == "Sizes and prices"  # type: ignore[attr-defined]


def test_rowspan_fills_the_rows_it_covers() -> None:
    """`Small` spans two rows, so the second row is `["Small", "38", "$12"]` -- not
    `["38", "$12"]`, which is what a naive row-by-row read produces and what makes
    the second row's columns misalign with the headers."""

    table = _one(SIZES_HTML)
    assert table.rows[0] == ["Small", "36", "$10"]  # type: ignore[attr-defined]
    assert table.rows[1] == ["Small", "38", "$12"]  # type: ignore[attr-defined]


def test_colspan_fills_the_columns_it_covers() -> None:
    table = _one(SIZES_HTML)
    assert table.rows[2] == ["Large", "Out of stock", "Out of stock"]  # type: ignore[attr-defined]


def test_every_row_has_the_same_width_as_the_headers() -> None:
    """The property the grid exists to guarantee: a consumer can zip rows against
    headers without checking lengths."""

    table = _one(SIZES_HTML)
    assert all(len(row) == len(table.headers) for row in table.rows)  # type: ignore[attr-defined]


def test_a_header_row_without_thead_is_recognised() -> None:
    table = _one("<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>")
    assert table.headers == ["A", "B"]  # type: ignore[attr-defined]
    assert table.rows == [["1", "2"]]  # type: ignore[attr-defined]


def test_a_first_row_of_data_is_not_stolen_for_headers() -> None:
    """crawl4ai falls back to "the first row, whatever it contains", so a table with
    no `<thead>` and no `<th>` silently loses its first row of data."""

    table = _one("<table><tr><td>1</td><td>2</td></tr><tr><td>3</td><td>4</td></tr></table>")
    assert table.headers == []  # type: ignore[attr-defined]
    assert table.rows == [["1", "2"], ["3", "4"]]  # type: ignore[attr-defined]


def test_a_nonsense_span_counts_as_one() -> None:
    """Read the way a browser reads it. `colspan="abc"` is not an error to raise
    over -- scraped HTML is full of them."""

    table = _one(
        '<table><tr><th>A</th><th>B</th></tr>'
        '<tr><td colspan="abc">1</td><td>2</td></tr></table>'
    )
    assert table.rows == [["1", "2"]]  # type: ignore[attr-defined]


def test_an_enormous_span_is_clamped() -> None:
    """Scraped HTML is untrusted and `rowspan * colspan` is a product: one cell
    claiming both at 99999 would otherwise allocate ten billion slots."""

    table = _one(
        '<table><tr><th>A</th></tr>'
        '<tr><td colspan="99999" rowspan="99999">x</td></tr></table>'
    )
    assert len(table.rows) == 1  # type: ignore[attr-defined]
    assert len(table.rows[0]) <= 1000  # type: ignore[attr-defined]


def test_a_short_row_does_not_leave_a_stale_span_behind() -> None:
    table = _one(
        "<table><tr><th>A</th><th>B</th><th>C</th></tr>"
        "<tr><td>1</td></tr>"
        "<tr><td>2</td><td>3</td><td>4</td></tr></table>"
    )
    assert table.rows[1] == ["2", "3", "4"]  # type: ignore[attr-defined]


# ----------------------------------------------------- data vs layout


def test_a_presentation_role_table_is_not_data() -> None:
    html = (
        '<table role="presentation"><tr><td><img src="x"></td>'
        '<td><a href="y">Home</a></td></tr></table>'
    )
    assert not is_data_table(lxml_html.fromstring(html))


def test_an_ordinary_data_table_is_data() -> None:
    assert is_data_table(lxml_html.fromstring(SIZES_HTML))


def test_an_icon_grid_is_not_data() -> None:
    """Mostly markup, no text, no headers -- the shape of a layout table."""

    cells = "".join(f'<td><img src="i{n}.png"><span></span></td>' for n in range(4))
    assert not is_data_table(lxml_html.fromstring(f"<table><tr>{cells}</tr></table>"))


def test_a_layout_wrapper_does_not_claim_its_nested_tables_caption() -> None:
    """`.//caption` on an email-layout table finds the caption of a table three
    levels in and reports it as the outer one's."""

    html = f"<table role='presentation'><tr><td>{SIZES_HTML}</td></tr></table>"
    outer = lxml_html.fromstring(html)
    assert extract_table(outer).caption is None


def test_a_real_table_inside_a_layout_table_is_still_found() -> None:
    """Each `<table>` is scored on its own merits, so the outer wrapper being junk
    does not hide the real one inside it."""

    html = f"<html><body><table role='presentation'><tr><td>{SIZES_HTML}</td></tr></table></body></html>"
    found = extract_tables(lxml_html.fromstring(html))
    assert len(found) == 1
    assert found[0].caption == "Available sizes"


def test_a_single_row_table_is_not_returned() -> None:
    """One row carries no relationships, which is the only thing a table format is
    for."""

    html = "<html><body><table><tr><td>just this</td><td>and this</td></tr></table></body></html>"
    assert extract_tables(lxml_html.fromstring(html)) == []


# ------------------------------------------------------ extractor wiring


def test_the_tables_format_returns_json() -> None:
    html = f"<html><body><article>{SIZES_HTML}</article></body></html>"
    payload = json.loads(extract(html, format="tables"))
    assert len(payload) == 1
    assert payload[0]["headers"] == ["Size", "EU", "Price"]
    assert payload[0]["row_count"] == 3
    assert payload[0]["column_count"] == 3


def test_the_tables_format_returns_an_empty_list_for_a_page_with_none() -> None:
    assert json.loads(extract("<html><body><p>no tables</p></body></html>", format="tables")) == []


def test_the_tables_format_falls_back_past_main_content_extraction() -> None:
    """A table inside a region the boilerplate selectors swallow should still be
    found, the same way the markdown path retries without main-content filtering."""

    html = f"<html><body><footer>{SIZES_HTML}</footer></body></html>"
    payload = json.loads(extract(html, format="tables", main_content=True))
    assert len(payload) == 1


def test_the_markdown_format_still_renders_tables_as_pipes() -> None:
    """`tables` is additive: the markdown rendering is unchanged, so a caller can
    ask for both and get the readable form and the usable one."""

    html = f"<html><body><article>{SIZES_HTML}</article></body></html>"
    markdown = extract(html, format="markdown")
    assert "| Size | EU | Price |" in markdown
