"""Deterministic entity extraction: the contact details, prices, dates and
identifiers on a page, found with regular expressions and nothing else.

This fills the gap between the two extraction paths that already exist.
`structured_data.py` reads what a page *declares* about itself (JSON-LD, OG
tags, hydration state) and finds nothing when a page declares nothing;
`ExtractConfig` calls a model, which finds anything but costs a round trip and
a per-page fee. An email address in a footer needs neither: it is a regular
expression, and charging a model call for it is the kind of cost that only
shows up at a million pages a month.

Adapted from crawl4ai's `RegexExtractionStrategy` (Apache-2.0; crawl4ai 0.9.4,
`crawl4ai/extraction_strategy.py`). The catalog below is deliberately smaller
than upstream's, because a pattern that fires on everything is worse than no
pattern -- it moves the filtering cost to the caller and hides the real matches
in noise:

* **Dropped `number` and `postal_us`.** `\\b\\d{5}(?:-\\d{4})?\\b` is "any
  five-digit number" and `number` is "any number at all". On a product page
  both return hundreds of matches, none of them a postcode.
* **Dropped `credit_card` and `iban`.** Not a capability worth shipping in a
  scraping service, whatever the pattern's accuracy.
* **Tightened `phone`, `url`, `ipv4` and `handle`.** Upstream's `phone_intl`
  (`\\+?\\d[\\d .()-]{7,}\\d`) matches any long run of digits and punctuation,
  which on a page with part numbers or timestamps is most of them; its `url`
  swallows the sentence's closing full stop; its `ipv4` accepts out-of-range
  octets and any position inside a longer dotted run; its `twitter_handle`
  matches the local part of every email address on the page.

  What none of them can do is tell `1.2.3.4` the address from `1.2.3.4` the
  version number -- that needs the surrounding sentence, not a better pattern.

Returns every label that matched, rather than taking a caller-supplied
selection: the whole pass is one regex sweep over text already in memory, so
the cost of finding all of them is indistinguishable from the cost of finding
one, and a `formats=["entities"]` request that needed a second parameter to
return anything useful would be a worse API.
"""

from __future__ import annotations

import re

_PATTERNS: dict[str, re.Pattern[str]] = {
    "email": re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]*[a-z]{2,}", re.IGNORECASE),
    # Requires real phone-number shape -- a leading `+`, or bracketed/separated
    # groups -- rather than any long digit run.
    "phone": re.compile(
        r"(?:\+\d{1,3}[ .-]?)?(?:\(\d{2,4}\)[ .-]?|\d{2,4}[ .-])"
        r"\d{2,4}[ .-]?\d{2,4}(?:[ .-]?\d{2,4})?"
    ),
    # The final class excludes sentence punctuation, so "see https://e.com/x."
    # yields the URL and not the full stop.
    "url": re.compile(
        r"https?://[^\s\"'<>()\]]*[^\s\"'<>()\].,;:!?]", re.IGNORECASE
    ),
    # Octet-range-validated, and bounded so a longer dotted run is not read as
    # an address with something after it: `lib 1.2.3.4.5` matches nothing.
    # A four-group version string (`1.2.3.4`) is genuinely indistinguishable
    # from an address by pattern alone, and is not claimed to be.
    "ipv4": re.compile(
        r"(?<![\d.])(?:(?:25[0-5]|2[0-4]\d|1\d{2}|[1-9]?\d)\.){3}"
        r"(?:25[0-5]|2[0-4]\d|1\d{2}|[1-9]?\d)(?![\d.])"
    ),
    "ipv6": re.compile(r"\b(?:[0-9a-f]{1,4}:){7}[0-9a-f]{1,4}\b", re.IGNORECASE),
    "mac_address": re.compile(r"\b(?:[0-9a-f]{2}:){5}[0-9a-f]{2}\b", re.IGNORECASE),
    "uuid": re.compile(
        r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
        re.IGNORECASE,
    ),
    "price": re.compile(
        r"(?:USD|EUR|GBP|INR|AUD|CAD|JPY|RM|[$€£¥₹])\s?\d{1,3}(?:[,\s]\d{3})*(?:\.\d{1,2})?\b"
    ),
    "percentage": re.compile(r"\b\d{1,3}(?:\.\d+)?\s?%"),
    "date_iso": re.compile(r"\b\d{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])\b"),
    "date_us": re.compile(r"\b(?:0?[1-9]|1[0-2])/(?:0?[1-9]|[12]\d|3[01])/(?:\d{2}|\d{4})\b"),
    "time_24h": re.compile(r"\b(?:[01]?\d|2[0-3]):[0-5]\d(?::[0-5]\d)?\b"),
    "hex_color": re.compile(r"#(?:[0-9a-f]{6}|[0-9a-f]{3})\b", re.IGNORECASE),
    "hashtag": re.compile(r"(?<![\w&])#[a-z][\w-]{1,49}\b", re.IGNORECASE),
    # The lookbehind is what stops this matching `bisht` out of
    # `rahul@bisht.com`, which upstream's `@[\w]{1,15}` does on every page
    # that lists an email address.
    "handle": re.compile(r"(?<![\w.@/])@[a-z][\w.]{2,29}\b", re.IGNORECASE),
}

_MAX_PER_LABEL = 500
"""A page with more matches than this for one label is a listing page, not a
page with contact details on it -- past a few hundred the marginal match is
noise and the response is paying to carry it."""


def extract_entities(text: str) -> dict[str, list[str]]:
    """Every entity kind with at least one match, each deduplicated and in the
    order it first appears.

    Takes text (markdown, in practice) rather than HTML: run over raw markup
    these patterns match inside attribute values, inline scripts and CSS, so
    `hex_color` returns a stylesheet's palette and `url` returns every tracking
    pixel. Markdown keeps the one piece of markup that matters -- a link's
    target, which survives as `(https://...)` -- and drops the rest.
    """

    if not text:
        return {}

    found: dict[str, list[str]] = {}
    for label, pattern in _PATTERNS.items():
        seen: dict[str, None] = {}
        for match in pattern.finditer(text):
            value = match.group(0).strip()
            if value and value not in seen:
                seen[value] = None
                if len(seen) >= _MAX_PER_LABEL:
                    break
        if seen:
            found[label] = list(seen)
    return found
