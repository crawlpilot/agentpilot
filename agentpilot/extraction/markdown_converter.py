"""Stage 2 of the extraction pipeline: sanitized `lxml.html` tree -> Markdown.

A small hand-rolled element-walk converter, ported rule-by-rule from
Firecrawl's vendored Go `html-to-markdown` fork -- commonmark.go's core rules
plus plugin/table.go's GFM tables and plugin/robust_code_block.go's
language-aware fenced code blocks -- rather than a generic library.
Firecrawl's own plugins exist specifically because generic converters
(Turndown/markdownify) get tables and code blocks wrong; adopting one here
would mean overriding most of it anyway. Not a full CommonMark
implementation -- covers the tag set Firecrawl actually handles.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lxml.html import HtmlElement

_GUTTER_RE = re.compile(r"gutter|line-numbers", re.IGNORECASE)
_WS_RE = re.compile(r"\s+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")

_BLOCK_TAGS = frozenset(
    {
        "p",
        "div",
        "li",
        "tr",
        "table",
        "thead",
        "tbody",
        "tfoot",
        "section",
        "article",
        "blockquote",
        "pre",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
    }
)


def to_markdown(root: HtmlElement) -> str:
    rendered = _render(root, 0)
    rendered = _BLANK_LINES_RE.sub("\n\n", rendered)
    return rendered.strip() + "\n" if rendered.strip() else ""


def _clean_text(text: str | None) -> str:
    return _WS_RE.sub(" ", text) if text else ""


def _render(el: HtmlElement, list_depth: int) -> str:
    if not isinstance(el.tag, str):
        return ""
    handler = _HANDLERS.get(el.tag.lower())
    if handler:
        return handler(el, list_depth)
    return _render_children(el, list_depth)


def _render_children(el: HtmlElement, list_depth: int) -> str:
    parts: list[str] = [_clean_text(el.text)]
    for child in el:
        parts.append(_render(child, list_depth))
        parts.append(_clean_text(child.tail))
    return "".join(parts)


def _heading(level: int) -> Callable[[HtmlElement, int], str]:
    prefix = "#" * level

    def handler(el: HtmlElement, list_depth: int) -> str:
        inner = _render_children(el, list_depth).strip()
        return f"{prefix} {inner}\n\n" if inner else ""

    return handler


def _paragraph(el: HtmlElement, list_depth: int) -> str:
    inner = _render_children(el, list_depth).strip()
    return f"{inner}\n\n" if inner else ""


def _wrap(marker: str) -> Callable[[HtmlElement, int], str]:
    def handler(el: HtmlElement, list_depth: int) -> str:
        inner = _render_children(el, list_depth).strip()
        return f"{marker}{inner}{marker}" if inner else ""

    return handler


def _br(el: HtmlElement, list_depth: int) -> str:
    return "  \n"


def _hr(el: HtmlElement, list_depth: int) -> str:
    return "\n\n---\n\n"


def _link(el: HtmlElement, list_depth: int) -> str:
    href = el.get("href") or ""
    inner = _render_children(el, list_depth).strip() or href
    title = el.get("title")
    if title:
        return f'[{inner}]({href} "{title}")'
    return f"[{inner}]({href})"


def _image(el: HtmlElement, list_depth: int) -> str:
    src = el.get("src") or ""
    alt = el.get("alt") or ""
    title = el.get("title")
    if title:
        return f'![{alt}]({src} "{title}")'
    return f"![{alt}]({src})"


def _blockquote(el: HtmlElement, list_depth: int) -> str:
    inner = _render_children(el, list_depth).strip()
    if not inner:
        return ""
    quoted = "\n".join(f"> {line}" if line else ">" for line in inner.split("\n"))
    return f"{quoted}\n\n"


def _render_list(ordered: bool) -> Callable[[HtmlElement, int], str]:
    def handler(el: HtmlElement, list_depth: int) -> str:
        lines: list[str] = []
        indent = "  " * list_depth
        index = 1
        for li in el:
            if not isinstance(li.tag, str) or li.tag.lower() != "li":
                continue
            marker = f"{index}." if ordered else "-"
            index += 1
            content = _render_children(li, list_depth + 1).strip()
            content_lines = content.split("\n") if content else [""]
            first, *rest = content_lines
            lines.append(f"{indent}{marker} {first}".rstrip())
            for extra in rest:
                lines.append(f"{indent}  {extra}" if extra else "")
        return "\n".join(lines) + "\n\n" if lines else ""

    return handler


# --- tables (ported from plugin/table.go) ---


def _direct_rows(table_el: HtmlElement) -> list[tuple[HtmlElement, bool]]:
    """Direct `<tr>` children (and `<thead>`/`<tbody>`/`<tfoot>` sections),
    skipping any nested tables. Returns `(tr, from_thead)` pairs."""
    rows: list[tuple[HtmlElement, bool]] = []
    for child in table_el:
        if not isinstance(child.tag, str):
            continue
        tag = child.tag.lower()
        if tag == "tr":
            rows.append((child, False))
        elif tag in ("thead", "tbody", "tfoot"):
            is_head = tag == "thead"
            for tr in child:
                if isinstance(tr.tag, str) and tr.tag.lower() == "tr":
                    rows.append((tr, is_head))
    return rows


def _row_cells(tr: HtmlElement) -> list[HtmlElement]:
    return [c for c in tr if isinstance(c.tag, str) and c.tag.lower() in ("td", "th")]


def _render_row(cells: list[HtmlElement], ncols: int) -> str:
    texts = [_render_children(c, 0).strip() for c in cells]
    while len(texts) < ncols:
        texts.append("")
    escaped = [t.replace("|", "\\|") for t in texts]
    return "| " + " | ".join(escaped) + " |"


def _render_table(el: HtmlElement, list_depth: int) -> str:
    rows = _direct_rows(el)
    if not rows:
        return ""

    row_cells = [(_row_cells(tr), is_head) for tr, is_head in rows]
    ncols = max((len(cells) for cells, _ in row_cells), default=0)
    if ncols == 0:
        return ""

    header_idx: int | None = None
    for i, (cells, is_head) in enumerate(row_cells):
        all_th = bool(cells) and all(c.tag.lower() == "th" for c in cells)
        if is_head or (i == 0 and all_th):
            header_idx = i
            break

    separator = "|" + " --- |" * ncols
    lines: list[str] = []
    if header_idx is None:
        lines.append("|" + "     |" * ncols)
        lines.append(separator)
        for cells, _ in row_cells:
            lines.append(_render_row(cells, ncols))
    else:
        for i, (cells, _) in enumerate(row_cells):
            lines.append(_render_row(cells, ncols))
            if i == header_idx:
                lines.append(separator)

    return "\n\n" + "\n".join(lines) + "\n\n"


# --- code blocks (ported from plugin/robust_code_block.go) ---


def _detect_lang(el: HtmlElement | None) -> str:
    if el is None:
        return ""
    classes = (el.get("class") or "").lower()
    for part in classes.split():
        if part.startswith("language-"):
            return part[len("language-") :]
        if part.startswith("lang-"):
            return part[len("lang-") :]
    return ""


def _collect_node(el: HtmlElement, parts: list[str]) -> None:
    """Mirrors Go's `collect(n, b)`: skip syntax-highlighter gutter/
    line-number spans, insert newlines after `<br>` and block-ish tags."""
    tag = el.tag.lower() if isinstance(el.tag, str) else None
    if tag:
        if _GUTTER_RE.search(el.get("class") or ""):
            return
        if tag == "br":
            parts.append("\n")
    if el.text:
        parts.append(el.text)
    for child in el:
        if isinstance(child.tag, str):
            _collect_node(child, parts)
        if child.tail:
            parts.append(child.tail)
    if tag in _BLOCK_TAGS:
        parts.append("\n")


def _calculate_fence(content: str) -> str:
    longest = current = 0
    for ch in content:
        if ch == "`":
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return "`" * max(longest + 1, 3)


def _code_block(el: HtmlElement, list_depth: int) -> str:
    code_el = next(iter(el.iter("code")), None)
    lang = _detect_lang(code_el) or _detect_lang(el)
    parts: list[str] = []
    _collect_node(el, parts)
    content = "".join(parts).rstrip("\n")
    fence = _calculate_fence(content)
    return f"\n\n{fence}{lang}\n{content}\n{fence}\n\n"


def _has_ancestor(el: HtmlElement, tag_name: str) -> bool:
    parent = el.getparent()
    while parent is not None:
        if isinstance(parent.tag, str) and parent.tag.lower() == tag_name:
            return True
        parent = parent.getparent()
    return False


def _inline_code(el: HtmlElement, list_depth: int) -> str:
    if _has_ancestor(el, "pre"):
        return ""  # the enclosing <pre> rule already rendered this
    parts: list[str] = []
    _collect_node(el, parts)
    code = "".join(parts).replace("\r\n", "\n").rstrip()
    fence = "`"
    if "`" in code:
        fence = "``"
        if "``" in code:
            fence = "```"
    return f"{fence}{code}{fence}"


_HANDLERS: dict[str, Callable[[HtmlElement, int], str]] = {
    "h1": _heading(1),
    "h2": _heading(2),
    "h3": _heading(3),
    "h4": _heading(4),
    "h5": _heading(5),
    "h6": _heading(6),
    "p": _paragraph,
    "br": _br,
    "hr": _hr,
    "strong": _wrap("**"),
    "b": _wrap("**"),
    "em": _wrap("*"),
    "i": _wrap("*"),
    "del": _wrap("~~"),
    "s": _wrap("~~"),
    "strike": _wrap("~~"),
    "a": _link,
    "img": _image,
    "ul": _render_list(ordered=False),
    "ol": _render_list(ordered=True),
    "blockquote": _blockquote,
    "pre": _code_block,
    "code": _inline_code,
    "table": _render_table,
}
