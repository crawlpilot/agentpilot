"""Deterministic structured-data extraction: JSON-LD, meta/OG/Twitter/Dublin-Core
tags, and Next.js/Nuxt/SPA hydration-state script tags.

Runs on a *fresh* `lxml.html` parse of the raw HTML -- deliberately not
`sanitizer.sanitize()`'s tree, since `sanitizer._drop_always()` strips
`head`/`meta`/`script` before this module would ever see them. No LLM, no
network -- pure transform, same discipline as the rest of `agentpilot.extraction`.
"""

from __future__ import annotations

import json
import re
from typing import Any, cast
from urllib.parse import urljoin

from lxml import html as lxml_html
from lxml.html import HtmlElement

_OG_TWITTER_DC_SELECTOR = (
    'meta[property^="og:"], meta[property^="article:"], '
    'meta[name^="twitter:"], meta[name^="dc."], meta[name^="dcterms."]'
)

_HYDRATION_SCRIPT_IDS = ("__NEXT_DATA__", "__NUXT_DATA__")

_HYDRATION_ASSIGNMENT_RE = re.compile(
    r"window\.(__NUXT__|__INITIAL_STATE__|__APOLLO_STATE__|__REDUX_STATE__)\s*=\s*"
)


def _select(root: HtmlElement, selector: str) -> list[HtmlElement]:
    try:
        return cast("list[HtmlElement]", root.cssselect(selector))
    except Exception:
        return []


def extract_json_ld(root: HtmlElement) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for script in _select(root, 'script[type="application/ld+json"]'):
        text = script.text_content()
        if not text or not text.strip():
            continue
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            continue
        results.extend(_flatten_json_ld(parsed))
    return results


def _flatten_json_ld(parsed: Any) -> list[dict[str, Any]]:
    """A JSON-LD block may itself be a list, or a single object with an
    `@graph` array bundling multiple entities -- flatten both into a flat
    list of dicts, dropping anything that isn't a dict (malformed input)."""
    if isinstance(parsed, list):
        items = parsed
    elif isinstance(parsed, dict) and isinstance(parsed.get("@graph"), list):
        items = parsed["@graph"]
    else:
        items = [parsed]
    return [item for item in items if isinstance(item, dict)]


def extract_meta(root: HtmlElement, base_url: str | None = None) -> dict[str, Any]:
    meta: dict[str, Any] = {}

    title_els = _select(root, "title")
    title = (
        title_els[0].text_content().strip()
        if title_els and title_els[0].text_content()
        else None
    )

    for el in _select(root, _OG_TWITTER_DC_SELECTOR):
        key = el.get("property") or el.get("name")
        content = el.get("content")
        if not key or content is None:
            continue
        _set_meta_value(meta, key, content)

    if not title:
        title = meta.get("og:title") or meta.get("twitter:title")
    if title:
        meta["title"] = title

    lang_els = _select(root, "html[lang]")
    if lang_els:
        lang = lang_els[0].get("lang")
        if lang:
            meta["language"] = lang

    favicon_els = _select(root, 'link[rel*="icon"]')
    if favicon_els:
        href = favicon_els[0].get("href")
        if href:
            meta["favicon"] = urljoin(base_url, href) if base_url else href

    # Generic pass: anything not already captured, keyed by whatever
    # attribute identifies it (name/property/itemprop), so arbitrary/custom
    # meta tags are still exposed rather than silently dropped.
    for el in _select(root, "meta"):
        key = el.get("name") or el.get("property") or el.get("itemprop")
        content = el.get("content")
        if not key or content is None or key in meta:
            continue
        _set_meta_value(meta, key, content)

    return meta


def _set_meta_value(meta: dict[str, Any], key: str, value: str) -> None:
    """Duplicate keys (e.g. multiple `og:locale:alternate` tags) become a
    list; `description`-like keys are joined instead of listed, matching
    Firecrawl's own `_extract_metadata` behavior."""
    if key not in meta:
        meta[key] = value
        return
    existing = meta[key]
    if "description" in key:
        meta[key] = f"{existing}, {value}" if isinstance(existing, str) else value
    elif isinstance(existing, list):
        existing.append(value)
    else:
        meta[key] = [existing, value]


def _extract_balanced_json(text: str, start: int) -> str | None:
    """Scan forward from `start` (which must point at a `{`) to find the
    matching closing brace, respecting string literals -- a naive non-greedy
    regex (`\\{.*?\\}`) stops at the *first* `}`, which truncates any
    realistically-nested hydration payload."""
    if start >= len(text) or text[start] != "{":
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _scan_hydration_globals(text: str) -> dict[str, Any]:
    found: dict[str, Any] = {}
    for match in _HYDRATION_ASSIGNMENT_RE.finditer(text):
        json_text = _extract_balanced_json(text, match.end())
        if json_text is None:
            continue
        try:
            found[match.group(1)] = json.loads(json_text)
        except json.JSONDecodeError:
            continue
    return found


def extract_hydration_state(root: HtmlElement) -> dict[str, Any]:
    hydration: dict[str, Any] = {}

    for script_id in _HYDRATION_SCRIPT_IDS:
        els = _select(root, f"script#{script_id}")
        if not els:
            continue
        text = els[0].text_content()
        if not text or not text.strip():
            continue
        try:
            hydration[script_id] = json.loads(text)
        except json.JSONDecodeError:
            continue

    # Inline `window.__X__ = {...}` assignment scripts (Nuxt and various
    # hand-rolled SPA bootstraps) -- no stable id/selector to target, so scan
    # raw script text for the assignment pattern instead. Runs regardless of
    # whether a static script-tag id was already found above, since a page
    # can legitimately set more than one of these independently.
    for script in _select(root, "script"):
        text = script.text_content()
        if not text:
            continue
        hydration.update(_scan_hydration_globals(text))

    return hydration


def extract_structured_data(html: str, *, base_url: str | None = None) -> dict[str, Any]:
    if not html or not html.strip():
        return {"metadata": {}, "json_ld": [], "hydration": {}}

    root = lxml_html.fromstring(html)
    return {
        "metadata": extract_meta(root, base_url),
        "json_ld": extract_json_ld(root),
        "hydration": extract_hydration_state(root),
    }
