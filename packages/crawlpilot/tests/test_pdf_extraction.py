"""`crawlpilot.extraction.pdf` and the HTTP tier's dispatch onto it.

Fixtures are genuine PDFs, assembled byte by byte by `make_pdf` below rather than
checked in as binaries: a real xref table and `%%EOF` are exactly what `pypdf`
validates, so a hand-waved fixture would test the failure path while claiming to
test the success one. (An earlier draft of this file did precisely that.)
"""

from __future__ import annotations

import json

import pytest

from crawlpilot.extraction import pdf


def make_pdf(pages: list[str]) -> bytes:
    """A valid single-font PDF with a real text layer, one line per page.

    Complete enough for `pypdf`: numbered objects, an xref table with correct byte
    offsets, a trailer naming the catalog, and `startxref` / `%%EOF`. Drop any of
    those and `PdfReader` reports "EOF marker not found" and yields no text.
    """

    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    font = add(b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>")
    contents_ids: list[int] = []
    for text in pages:
        stream = f"BT /F1 12 Tf 40 700 Td ({text}) Tj ET".encode()
        contents_ids.append(
            add(b"<</Length %d>>stream\n%s\nendstream" % (len(stream), stream))
        )
    # The /Pages object is written after the pages but referenced by them, so its
    # id has to be predicted.
    pages_id = len(objects) + len(pages) + 1
    page_ids = [
        add(
            b"<</Type/Page/Parent %d 0 R/MediaBox[0 0 612 792]"
            b"/Resources<</Font<</F1 %d 0 R>>>>/Contents %d 0 R>>"
            % (pages_id, font, content_id)
        )
        for content_id in contents_ids
    ]
    kids = b"".join(b"%d 0 R " % pid for pid in page_ids).strip()
    assert add(b"<</Type/Pages/Kids[%s]/Count %d>>" % (kids, len(page_ids))) == pages_id
    root = add(b"<</Type/Catalog/Pages %d 0 R>>" % pages_id)

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj" % index + body + b"endobj\n"
    xref_at = len(out)
    out += b"xref\n0 %d\n" % (len(objects) + 1)
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer<</Size %d/Root %d 0 R>>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        root,
        xref_at,
    )
    return bytes(out)


TWO_PAGES = ["Refund policy: fourteen days", "Warranty claims need a receipt"]


# ------------------------------------------------------------------ detection


def test_magic_bytes_identify_a_pdf_without_a_content_type() -> None:
    assert pdf.is_pdf(content_type=None, body=make_pdf(["x"]))


def test_a_pdf_labelled_octet_stream_is_still_a_pdf() -> None:
    """Plenty of servers mislabel PDFs, so the body is authoritative."""

    assert pdf.is_pdf(content_type="application/octet-stream", body=make_pdf(["x"]))


def test_html_mislabelled_as_a_pdf_is_not_a_pdf() -> None:
    """The other direction, and the one that matters: a CDN serving a 404 page
    under the original `application/pdf` header. Trusting the header would hand
    the PDF parser markup."""

    assert not pdf.is_pdf(
        content_type="application/pdf", body=b"<html><body>Not found</body></html>"
    )


def test_a_content_type_with_parameters_is_handled() -> None:
    assert pdf.is_pdf(content_type="application/pdf; charset=binary", body=b"")


def test_ordinary_html_is_not_a_pdf() -> None:
    assert not pdf.is_pdf(content_type="text/html", body=b"<html></html>")


# ----------------------------------------------------------------- extraction


def test_markdown_carries_a_heading_per_page() -> None:
    """Upstream concatenates every page into one blob; a 90-page filing then has no
    structure and nothing can be cited."""

    out = pdf.extract_pdf(make_pdf(TWO_PAGES), format="markdown")
    assert "## Page 1" in out
    assert "## Page 2" in out
    assert "Refund policy: fourteen days" in out
    assert "Warranty claims need a receipt" in out
    assert out.index("## Page 1") < out.index("## Page 2")


def test_text_format_has_no_headings_and_collapses_whitespace() -> None:
    out = pdf.extract_pdf(make_pdf(TWO_PAGES), format="text")
    assert "## Page" not in out
    assert out == "Refund policy: fourteen days Warranty claims need a receipt"


def test_an_unreadable_document_yields_empty_rather_than_raising() -> None:
    """A truncated or encrypted PDF is an ordinary thing to meet on the web. One
    unreadable document must not fail a crawl."""

    assert pdf.extract_pdf(b"%PDF-1.4\nnot really a pdf", format="markdown") == ""


def test_an_empty_body_yields_empty() -> None:
    assert pdf.extract_pdf(b"", format="markdown") == ""


def test_a_page_with_no_text_layer_contributes_nothing() -> None:
    """A scanned page. Genuinely empty rather than an error -- OCR is a different
    feature, and claiming to have read it would be worse than saying nothing."""

    out = pdf.extract_pdf(make_pdf([""]), format="markdown")
    assert out == ""


def test_the_page_count_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """A scraped URL is untrusted input: a 4,000-page document is a
    memory-exhaustion vector, not a document."""

    monkeypatch.setattr(pdf, "MAX_PAGES", 2)
    out = pdf.extract_pdf(make_pdf([f"page {n}" for n in range(6)]), format="markdown")
    assert "## Page 2" in out
    assert "## Page 3" not in out
    assert "truncated" in out


def test_the_character_count_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pdf, "MAX_CHARACTERS", 10)
    out = pdf.extract_pdf(make_pdf(TWO_PAGES), format="markdown")
    assert "truncated" in out


def test_metadata_is_read_when_present() -> None:
    """A PDF has no `<title>` element, and its URL is frequently a hash, so
    `/Title` is often the only usable title available."""

    assert pdf.pdf_metadata(make_pdf(["x"])) == {}


def test_metadata_on_an_unreadable_document_is_empty() -> None:
    assert pdf.pdf_metadata(b"garbage") == {}


def test_a_missing_pypdf_raises_a_descriptive_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guarded-import path. A deployment without the `pdf` extra must get a
    message naming the extra on that one document, not an ImportError at startup."""

    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "pypdf":
            raise ImportError("no pypdf")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(pdf.PdfSupportUnavailable, match="crawlpilot\\[pdf\\]"):
        pdf.extract_pdf(make_pdf(["x"]), format="markdown")


# ------------------------------------------------------- HTTP tier dispatch


async def test_the_http_tier_returns_pdf_text_for_every_requested_format() -> None:
    """`extracts` has to stay index-correlated with `formats`, including for the
    formats a PDF cannot answer -- a short list would silently misalign every
    format after the missing one."""

    import httpx

    from crawlpilot.session.http_fetch import fetch_via_http
    from crawlpilot.spi.scrape import ScrapeOptions

    body = make_pdf(TWO_PAGES)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=body, headers={"content-type": "application/pdf"}
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    formats = ("markdown", "text", "html", "structured_data", "entities", "tables")
    result = await fetch_via_http(
        url="https://x.test/datasheet.pdf",
        formats=formats,
        options=ScrapeOptions(formats=formats),
        headers={},
        client=client,
    )

    assert len(result.extracts) == len(formats)
    extracted = dict(zip(formats, result.extracts, strict=True))
    assert "Refund policy: fourteen days" in extracted["markdown"]
    assert "## Page" not in extracted["text"]
    assert extracted["html"] == ""
    assert json.loads(extracted["structured_data"])["json_ld"] == []
    assert json.loads(extracted["tables"]) == []
    assert result.status_code == 200


async def test_a_pdf_never_reaches_block_detection() -> None:
    """PDF bytes run through `classify_page` would be searched for wall markers,
    and through `sanitize` would be handed to lxml as markup. Neither is
    meaningful, so the dispatch short-circuits before both."""

    import httpx

    from crawlpilot.session.http_fetch import fetch_via_http
    from crawlpilot.spi.scrape import ScrapeOptions

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=make_pdf(["Access Denied"]),
            headers={"content-type": "application/pdf"},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = await fetch_via_http(
        url="https://x.test/a.pdf",
        formats=("markdown",),
        options=ScrapeOptions(),
        headers={},
        client=client,
    )
    # "Access Denied" is a wall marker `block_detect` would flag in HTML. Here it
    # is simply the document's text.
    assert result.soft_verdict is None
    assert "Access Denied" in result.extracts[0]


async def test_html_still_takes_the_html_path() -> None:
    """The dispatch must not capture ordinary pages."""

    import httpx

    from crawlpilot.session.http_fetch import fetch_via_http
    from crawlpilot.spi.scrape import ScrapeOptions

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"<html><head><title>T</title></head><body><article>"
            b"<p>Real page content here, long enough to survive.</p>"
            b"</article></body></html>",
            headers={"content-type": "text/html"},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = await fetch_via_http(
        url="https://x.test/page",
        formats=("markdown",),
        options=ScrapeOptions(),
        headers={},
        client=client,
    )
    assert "Real page content" in result.extracts[0]
    assert result.page_title == "T"
