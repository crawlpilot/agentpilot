"""Stage 1 of the extraction pipeline: HTML sanitization / main-content
extraction. Ported from Firecrawl's `transform_html` (apps/api/native/src/
html.rs) as pure Python over `lxml.html`, minus the proprietary OMCE
learned-signature boilerplate remover (DB-backed, not portable).

Pure transform: takes an HTML string, returns a cleaned `lxml.html` tree for
`markdown_converter.py`/`extractor.py`'s text path to consume directly --
no serialize/reparse round trip between stages.
"""

from __future__ import annotations

import logging
import re
from typing import cast
from urllib.parse import urljoin

from lxml import html as lxml_html
from lxml.html import HtmlElement

from agentpilot.extraction import selectors

log = logging.getLogger(__name__)

_ALWAYS_STRIP = ("head", "meta", "noscript", "style", "script")
_SRCSET_DESCRIPTOR_RE = re.compile(r"^\d+(?:\.\d+)?[wx]$")


def sanitize(
    html: str,
    *,
    only_main_content: bool,
    include_tags: tuple[str, ...] | None = None,
    exclude_tags: tuple[str, ...] | None = None,
    base_url: str | None = None,
) -> HtmlElement:
    if not html or not html.strip():
        return lxml_html.fromstring("<div></div>")

    root = lxml_html.fromstring(html)
    effective_base = _resolve_base_href(root, base_url)

    if include_tags:
        root = _keep_only_include_tags(root, include_tags)

    _drop_always(root)

    if exclude_tags:
        _remove_by_selectors(root, exclude_tags)

    if only_main_content:
        _remove_boilerplate(root)

    _resolve_lazy_images(root)
    _absolutify(root, effective_base)

    return root


def _select(root: HtmlElement, selector: str) -> list[HtmlElement]:
    try:
        return cast("list[HtmlElement]", root.cssselect(selector))
    except Exception:
        log.warning("extraction.sanitizer.invalid_selector", extra={"selector": selector})
        return []


def _keep_only_include_tags(root: HtmlElement, include_tags: tuple[str, ...]) -> HtmlElement:
    new_root = lxml_html.fromstring("<div></div>")
    for selector in include_tags:
        for match in _select(root, selector):
            new_root.append(match)
    return new_root


def _drop_always(root: HtmlElement) -> None:
    for tag in _ALWAYS_STRIP:
        for el in list(root.iter(tag)):
            el.drop_tree()


def _remove_by_selectors(root: HtmlElement, sels: tuple[str, ...]) -> None:
    for selector in sels:
        for el in _select(root, selector):
            if el.getparent() is not None:
                el.drop_tree()


def _remove_boilerplate(root: HtmlElement) -> None:
    to_drop: list[HtmlElement] = []
    for selector in selectors.BOILERPLATE_SELECTORS:
        for el in _select(root, selector):
            if not _contains_force_include(el):
                to_drop.append(el)
    for el in to_drop:
        if el.getparent() is not None:
            el.drop_tree()


def _contains_force_include(el: HtmlElement) -> bool:
    for selector in selectors.FORCE_INCLUDE_SELECTORS:
        try:
            if el.cssselect(selector):
                return True
        except Exception:
            continue
    return False


def _resolve_base_href(root: HtmlElement, base_url: str | None) -> str | None:
    base_els = _select(root, "base[href]")
    if base_els:
        href = base_els[0].get("href")
        if href and base_url:
            try:
                return str(urljoin(base_url, href))
            except ValueError:
                return base_url
    return base_url


def _resolve_lazy_images(root: HtmlElement) -> None:
    for img in root.iter("img"):
        srcset = img.get("srcset") or img.get("data-srcset")
        if srcset and srcset.strip():
            best = _pick_largest_srcset(srcset)
            if best:
                img.set("src", best)
                continue
        data_src = img.get("data-src")
        if data_src and data_src.strip():
            img.set("src", data_src)


def _pick_largest_srcset(srcset: str) -> str | None:
    candidates: list[tuple[float, str]] = []
    for part in srcset.split(","):
        tokens = part.strip().split()
        if not tokens:
            continue
        if len(tokens) > 1 and _SRCSET_DESCRIPTOR_RE.match(tokens[-1]):
            url = " ".join(tokens[:-1])
            size = float(tokens[-1][:-1])
        else:
            url = " ".join(tokens)
            size = 1.0
        candidates.append((size, url))
    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0], reverse=True)
    return candidates[0][1]


def _absolutify(root: HtmlElement, base_url: str | None) -> None:
    if not base_url:
        return
    try:
        root.make_links_absolute(base_url, resolve_base_href=False)
    except Exception:
        log.warning("extraction.sanitizer.absolutify_failed")
