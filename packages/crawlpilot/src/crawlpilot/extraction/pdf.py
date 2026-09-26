"""PDF text extraction: a scraped `.pdf` as markdown, rather than as nothing.

Until now "pdf" appeared in this codebase only as a thing to avoid -- a
`resource_type` to block, a file extension to deny in `crawl.filters`. Scraping a
PDF URL produced an empty document. That is a real gap rather than a cosmetic one,
because a great deal of what callers actually want is in PDFs: datasheets,
annual reports, published price lists, government and regulatory filings, academic
papers. All of them are linked from ordinary HTML pages that the crawler happily
follows to a dead end.

**The HTTP tier is the only one that can do this.** Chrome does not hand a PDF's
text to `page.content()`; it renders the file in its built-in viewer, so the
browser path sees the *viewer's* markup and no document text at all. So dispatch
happens in `session/http_fetch.py`, on the response's content type, and the
browser tier is not involved. A PDF behind a WAF that requires a real browser is
therefore still out of reach -- named here rather than silently half-working.

Adapted from crawl4ai's `PDFContentScrapingStrategy` / `processors/pdf`
(Apache-2.0; crawl4ai 0.9.4). Three deviations:

1. **`pypdf` is an optional extra, imported inside the function.** crawlpilot's
   base install promises "no browser, no framework"; a document parser is neither,
   and a deployment that never scrapes a PDF should not carry it. Missing, it
   produces a descriptive error on that one document rather than an `ImportError`
   at startup.
2. **Page text is joined with a heading per page.** Upstream concatenates pages
   into one blob. A 90-page filing then has no structure at all, and a caller
   cannot cite where anything came from. `## Page 4` costs four tokens and makes
   the output navigable.
3. **Bounded.** `MAX_PAGES` and `MAX_CHARACTERS` exist because a scraped URL is
   untrusted input: a 4,000-page PDF is a memory-exhaustion vector, not a
   document, and truncating with a visible marker beats the worker being
   OOM-killed mid-crawl.
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

MAX_PAGES = 200
"""Pages read before truncating. Generous for real documents -- a long annual
report is 150 -- and a hard bound against a crafted one."""

MAX_CHARACTERS = 2_000_000
"""Roughly 500k tokens. Past this the caller cannot use the text in one piece
anyway, and holding it costs the worker memory it needs for the rest of the
crawl."""

PDF_CONTENT_TYPES = frozenset({"application/pdf", "application/x-pdf"})

PDF_MAGIC = b"%PDF-"
"""Every PDF starts with this. Checked as well as the content type because plenty
of servers label a PDF `application/octet-stream`, and a few label HTML
`application/pdf`."""

_BLANK_RUN = re.compile(r"\n{3,}")


def is_pdf(*, content_type: str | None, body: bytes) -> bool:
    """Whether this response is a PDF.

    The magic bytes are authoritative and the content type is a hint: a response
    whose header says PDF and whose body is an HTML error page is common enough
    (a CDN serving a 404 with the original type) that trusting the header alone
    would hand the parser markup.
    """

    if body[:5] == PDF_MAGIC:
        return True
    if not content_type:
        return False
    declared = content_type.split(";", 1)[0].strip().lower()
    # Declared-but-unverified only counts when the body is too short to have
    # magic bytes at all, i.e. an empty or truncated response.
    return declared in PDF_CONTENT_TYPES and len(body) < 5


class PdfSupportUnavailable(RuntimeError):
    """`pypdf` is not installed. Raised rather than returning empty text so the
    caller can put a descriptive message on the document instead of a silent
    blank."""


def extract_pdf(body: bytes, *, format: str = "markdown") -> str:
    """A PDF's text, as markdown (with per-page headings) or as plain text.

    Never raises for a malformed document: a PDF that `pypdf` cannot parse, or
    one whose pages are scanned images with no text layer, yields an empty string.
    Both are ordinary things to encounter on the web, and neither should fail a
    crawl. `PdfSupportUnavailable` is the one exception, and it is about this
    deployment rather than about the document.
    """

    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - exercised by a monkeypatched test
        raise PdfSupportUnavailable(
            "PDF extraction requires the 'pdf' extra: pip install 'crawlpilot[pdf]'"
        ) from exc

    import io

    try:
        reader = PdfReader(io.BytesIO(body), strict=False)
        pages = reader.pages
    except Exception:
        # Encrypted, truncated, or simply not a PDF despite its header. One
        # unreadable document is not a crawl failure.
        log.warning("extraction.pdf.unreadable")
        return ""

    parts: list[str] = []
    total = 0
    for index, page in enumerate(pages):
        if index >= MAX_PAGES:
            parts.append(f"\n\n*[truncated: document has more than {MAX_PAGES} pages]*")
            break
        try:
            text = page.extract_text() or ""
        except Exception:
            # A single corrupt page in an otherwise-readable document. Skipping it
            # keeps the other 89 pages, which is the useful outcome.
            continue
        text = text.strip()
        if not text:
            # A scanned page with no text layer. Genuinely empty, not an error --
            # OCR would be a different feature.
            continue
        if format == "markdown":
            parts.append(f"## Page {index + 1}\n\n{text}")
        else:
            parts.append(text)
        total += len(text)
        if total >= MAX_CHARACTERS:
            parts.append("\n\n*[truncated: document exceeds the size limit]*")
            break

    joined = "\n\n".join(parts)
    if format != "markdown":
        return " ".join(joined.split())
    # PDF text extraction produces long runs of blank lines wherever the source
    # had layout whitespace; collapse them so the markdown is readable.
    return _BLANK_RUN.sub("\n\n", joined).strip()


def pdf_metadata(body: bytes) -> dict[str, str]:
    """The document's title and author, when it declares them.

    Worth reading separately: a PDF's `/Title` is often the only usable title for
    it, since there is no `<title>` element and the URL is frequently a hash.
    """

    try:
        from pypdf import PdfReader
    except ImportError:
        return {}

    import io

    info: dict[str, object] = {}
    try:
        reader = PdfReader(io.BytesIO(body), strict=False)
        info = dict(reader.metadata or {})
    except Exception:
        return {}

    out: dict[str, str] = {}
    for key, field in (("title", "/Title"), ("author", "/Author"), ("subject", "/Subject")):
        value = info.get(field)
        if isinstance(value, str) and value.strip():
            out[key] = value.strip()
    return out
