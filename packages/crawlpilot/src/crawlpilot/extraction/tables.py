"""Structured table extraction: a table's rows as data, not as pipe-markdown.

`markdown_converter._render_table` already renders tables into the markdown, and
for reading that is the right output. For *using* them it is not: the pipe format
flattens `colspan`/`rowspan` into ragged rows, drops the `<caption>` and the
`summary` attribute, and leaves the consumer to re-parse a table that was already
structured when it arrived. A caller extracting a price-by-size grid off a product
page ends up writing a markdown table parser, which is a strange thing to have to
do to data that started as `<td>`s.

So this returns the grid. `formats: ["tables"]` yields one object per table with
`headers`, `rows`, `caption` and `summary`, and the spans resolved into the cells
they actually cover.

The other half of the problem is that most `<table>` elements on the web are not
tables. Layout tables, icon grids, `role="presentation"` wrappers and email-style
nested tables all use the same tag, and returning them as data is worse than
returning nothing. `is_data_table` scores each one and the low scorers are
dropped.

Ported from crawl4ai's `DefaultTableExtraction` (Apache-2.0; crawl4ai 0.9.4,
`crawl4ai/table_extraction.py`). The grid-building approach and the scoring
heuristic are theirs, including the span clamping -- scraped HTML is untrusted and
`rowspan * colspan` is a product, so one crafted cell could otherwise make the
work explode. Two deviations, both marked at their sites:

1. **Cell selection never descends into nested tables.** Upstream's header pass
   uses `.//th` under `thead`, which reaches through a nested `<table>` and pulls
   its cells into the outer table's headers. The grid builder gets this right with
   `./th|./td`; the header pass did not.
2. **A header row is only taken from the first row when it looks like one.**
   Upstream falls back to "the first row, whatever it contains", so a table with
   no `<thead>` loses its first row of *data* to the header list.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

from lxml.html import HtmlElement

COLSPAN_LIMIT = 1000
"""The HTML Standard's cap, which is what browsers clamp to."""

DEFAULT_SCORE_THRESHOLD = 5.0
"""Tuned so an ordinary two-column data table with a header row passes and a
layout wrapper does not. Lower to keep more, raise to keep only obvious ones."""

MIN_ROWS = 2
"""A single-row `<table>` carries no relationships, which is the only thing a
table format is for."""

_PRESENTATION_ROLES = frozenset({"presentation", "none"})


@dataclass
class Table:
    headers: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)
    caption: str | None = None
    summary: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "headers": self.headers,
            "rows": self.rows,
            "caption": self.caption,
            "summary": self.summary,
            "row_count": len(self.rows),
            "column_count": len(self.headers) or (len(self.rows[0]) if self.rows else 0),
        }


def extract_tables(
    root: HtmlElement, *, score_threshold: float = DEFAULT_SCORE_THRESHOLD
) -> list[Table]:
    """Every data table in `root`, in document order.

    Nested tables are considered on their own merits: an outer layout table scores
    badly and is dropped, while a real table inside it is still found, because
    `iter` visits both and each is scored independently.
    """

    tables: list[Table] = []
    for element in root.iter("table"):
        if not is_data_table(element, score_threshold=score_threshold):
            continue
        table = extract_table(element)
        if len(table.rows) >= MIN_ROWS - (1 if table.headers else 0):
            tables.append(table)
    return tables


def is_data_table(table: HtmlElement, *, score_threshold: float = DEFAULT_SCORE_THRESHOLD) -> bool:
    """Whether this `<table>` holds data or is doing layout.

    Additive scoring rather than any single test, because no single signal is
    reliable: plenty of real tables lack a `<thead>`, and plenty of layout tables
    have consistent column counts.
    """

    rows = _direct_rows(table)
    if not rows:
        return False

    score = 0.0

    if table.get("role", "").lower() in _PRESENTATION_ROLES:
        # The author said outright that this is not a table. Worth a heavy penalty
        # rather than a veto, since the attribute is also applied carelessly.
        score -= 3
    if _has_nested_table(table):
        score -= 3

    if _select(table, "thead") or any(_cells(row, "th") for row in rows):
        score += 2
    if _select(table, "caption"):
        score += 2
    if table.get("summary"):
        score += 1

    cell_counts = [len(_cells(row)) for row in rows]
    average_columns = sum(cell_counts) / len(cell_counts)
    variance = sum((count - average_columns) ** 2 for count in cell_counts) / len(cell_counts)
    if variance < 1:
        # Ragged rows are the clearest layout-table tell; consistent ones are the
        # clearest data-table one.
        score += 2
    if len(rows) >= 2 and average_columns >= 2:
        score += 2

    text_length = sum(
        len(cell.text_content().strip()) for row in rows for cell in _cells(row)
    )
    tag_count = sum(1 for _ in table.iterdescendants())
    text_ratio = text_length / (tag_count + 1e-5)
    if text_ratio > 20:
        # A data table is mostly text with a little markup; a layout table is the
        # reverse, since its cells hold images, buttons and nested divs.
        score += 3
    elif text_ratio > 10:
        score += 2

    return score >= score_threshold


def extract_table(table: HtmlElement) -> Table:
    caption_elements = _select(table, "caption")
    caption = caption_elements[0].text_content().strip() if caption_elements else None
    summary = (table.get("summary") or "").strip() or None

    rows = _direct_rows(table)
    grid = _build_grid(rows)

    headers: list[str] = []
    body = grid
    head_rows = [row for row in rows if _in_thead(row)]

    if head_rows:
        header_count = len(head_rows)
        headers = grid[0] if grid else []
        body = grid[header_count:]
    elif grid and _looks_like_header(rows[0]):
        # Deviation 2: only when the first row is actually made of `<th>`s.
        # Upstream takes the first row regardless, which silently costs a
        # `<thead>`-less table its first row of data.
        headers = grid[0]
        body = grid[1:]

    return Table(
        headers=headers,
        rows=body,
        caption=caption or None,
        summary=summary,
    )


def _select(element: HtmlElement, tag: str) -> list[HtmlElement]:
    """Direct-descendant search that stops at a nested `<table>`.

    Deviation 1: `.//caption` on an email-layout table finds the caption of a
    table three levels in and reports it as this one's.
    """

    found: list[HtmlElement] = []
    for child in element:
        if not isinstance(child.tag, str):
            continue
        if child.tag == "table":
            continue
        if child.tag == tag:
            found.append(cast("HtmlElement", child))
        else:
            found.extend(_select(cast("HtmlElement", child), tag))
    return found


def _direct_rows(table: HtmlElement) -> list[HtmlElement]:
    """This table's own `<tr>`s -- not a nested table's."""

    return _select(table, "tr")


def _cells(row: HtmlElement, tag: str | None = None) -> list[HtmlElement]:
    """A row's own cells. `./th|./td`, never `.//`, so a cell containing a nested
    table does not contribute that table's cells to this row."""

    wanted = (tag,) if tag else ("th", "td")
    return [
        cast("HtmlElement", child)
        for child in row
        if isinstance(child.tag, str) and child.tag in wanted
    ]


def _has_nested_table(table: HtmlElement) -> bool:
    return any(descendant.tag == "table" for descendant in table.iterdescendants())


def _in_thead(row: HtmlElement) -> bool:
    parent = row.getparent()
    return parent is not None and isinstance(parent.tag, str) and parent.tag == "thead"


def _looks_like_header(row: HtmlElement) -> bool:
    cells = _cells(row)
    return bool(cells) and all(cell.tag == "th" for cell in cells)


def _span(value: str | None, limit: int) -> int:
    """Read a span attribute the way a browser does: junk counts as 1."""

    try:
        return min(max(int(value or 1), 1), limit)
    except (TypeError, ValueError):
        return 1


def _build_grid(rows: list[HtmlElement]) -> list[list[str]]:
    """Lay the cells into a rectangle, honouring `colspan` and `rowspan`.

    Each cell writes its text into every slot of the rectangle it spans, including
    slots in later rows, so a row only has to fill what the rows above left free.
    Nothing is carried between iterations, so a short row cannot leave a stale
    span behind.

    Both spans are clamped: scraped HTML is untrusted and `rowspan * colspan` is a
    product, so one cell claiming `colspan=99999 rowspan=99999` would otherwise
    allocate ten billion slots. A `rowspan` cannot reach past the last row, which
    is a tighter bound than the standard's own.
    """

    grid: list[list[str | None]] = [[] for _ in rows]

    def put(row_index: int, column: int, text: str) -> None:
        row = grid[row_index]
        row.extend([None] * (column + 1 - len(row)))
        row[column] = text

    for row_index, row in enumerate(rows):
        column = 0
        for cell in _cells(row):
            while column < len(grid[row_index]) and grid[row_index][column] is not None:
                column += 1  # taken by a rowspan from a row above
            text = cell.text_content().strip()
            colspan = _span(cell.get("colspan"), COLSPAN_LIMIT)
            rowspan = _span(cell.get("rowspan"), len(grid) - row_index)
            for row_offset in range(rowspan):
                for column_offset in range(colspan):
                    put(row_index + row_offset, column + column_offset, text)
            column += colspan

    return [[text or "" for text in row] for row in grid if row]
