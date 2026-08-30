"""Boilerplate/main-content CSS selector lists, ported verbatim from Firecrawl's
`EXCLUDE_NON_MAIN_TAGS`/`FORCE_INCLUDE_MAIN_TAGS` (apps/api/native/src/html.rs).

Kept as plain data, separate from sanitizer.py's tree-walking logic, so the
list is trivially diffable/reviewable on its own.
"""

from __future__ import annotations

BOILERPLATE_SELECTORS: tuple[str, ...] = (
    "header",
    "footer",
    "nav",
    "aside",
    ".header",
    ".top",
    ".navbar",
    "#header",
    ".footer",
    ".bottom",
    "#footer",
    ".sidebar",
    ".side",
    ".aside",
    "#sidebar",
    ".modal",
    ".popup",
    "#modal",
    ".overlay",
    ".ad",
    ".ads",
    ".advert",
    "#ad",
    ".lang-selector",
    ".language",
    "#language-selector",
    ".social",
    ".social-media",
    ".social-links",
    "#social",
    ".menu",
    ".navigation",
    "#nav",
    ".breadcrumbs",
    "#breadcrumbs",
    ".share",
    "#share",
    ".widget",
    "#widget",
    ".cookie",
    "#cookie",
    ".fc-decoration",
)

FORCE_INCLUDE_SELECTORS: tuple[str, ...] = (
    "#main",
    ".swoogo-cols",
    ".swoogo-text",
    ".swoogo-table-div",
    ".swoogo-space",
    ".swoogo-alert",
    ".swoogo-sponsors",
    ".swoogo-title",
    ".swoogo-tabs",
    ".swoogo-logo",
    ".swoogo-image",
    ".swoogo-button",
    ".swoogo-agenda",
)
