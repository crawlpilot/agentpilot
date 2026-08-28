"""The request headers a real Chrome sends, in the order it sends them.

The `basic` tier used to send five headers -- `User-Agent`, `Accept-Language`
and the three Client Hints -- and nothing else. No `Accept`, no `Sec-Fetch-*`,
no `Upgrade-Insecure-Requests`, no `Referer`. Every one of those is present on
every real top-level navigation Chrome makes, so their *absence* is a one-line
rule at any WAF edge, well before anything as subtle as a TLS fingerprint is
consulted.

Order matters too, and is why this builds a list rather than relying on a dict
literal. `httpx` emits headers in insertion order, so the old dict emitted
`User-Agent` first, which no browser does. Header order is itself a fingerprint
that WAFs hash alongside the TLS ClientHello.

Neither PulsarRPA nor Browser4 has anything here to port -- both run a real
Chrome for everything and never construct headers by hand (`NetworkManager
.setExtraHTTPHeaders` is used only for `Referer`). This is the layer their
"just use a real browser" answer skips, and which the `basic` tier cannot.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterable

# Chrome's actual top-level-navigation header order. `Host`/`Connection` are
# owned by the HTTP client and are not ours to set.
_ORDER = (
    "sec-ch-ua",
    "sec-ch-ua-mobile",
    "sec-ch-ua-platform",
    "upgrade-insecure-requests",
    "user-agent",
    "accept",
    "sec-fetch-site",
    "sec-fetch-mode",
    "sec-fetch-user",
    "sec-fetch-dest",
    "referer",
    "accept-encoding",
    "accept-language",
    "priority",
)

_NAVIGATION_ACCEPT = (
    "text/html,application/xhtml+xml,application/xml;q=0.9,"
    "image/avif,image/webp,image/apng,*/*;q=0.8,"
    "application/signed-exchange;v=b3;q=0.7"
)

CHROME_ACCEPT_ENCODING = "gzip, deflate, br, zstd"
"""What Chrome advertises."""


def _supported_accept_encoding() -> str:
    """Chrome's `Accept-Encoding`, reduced to what this process can actually
    decode.

    Advertising an encoding you cannot decode is worse than advertising a
    smaller set: the server honours the claim and you get undecodable bytes.
    `httpx` handles br/zstd only when `brotli`/`zstandard` are importable, so
    the honest header depends on what is installed. They are base dependencies
    precisely so this normally resolves to the full Chrome string -- a short
    `gzip, deflate` is itself a (mild) tell, since no browser sends it.
    """

    codings = ["gzip", "deflate"]
    for module, coding in (("brotli", "br"), ("brotlicffi", "br"), ("zstandard", "zstd")):
        if coding in codings:
            continue
        try:
            importlib.import_module(module)
        except ImportError:
            continue
        codings.append(coding)
    # Chrome's order is gzip, deflate, br, zstd.
    return ", ".join(sorted(codings, key=CHROME_ACCEPT_ENCODING.split(", ").index))


_ACCEPT_ENCODING = _supported_accept_encoding()


def navigation_headers(
    *,
    user_agent: str,
    accept_language: str,
    client_hints: dict[str, str],
    referer: str | None = None,
    accept_encoding: str = _ACCEPT_ENCODING,
) -> dict[str, str]:
    """Headers for a top-level document navigation, in Chrome's order.

    `Sec-Fetch-Site` is derived from whether a referer is present: a hit with
    no referer is `none` (an address-bar navigation), and one carrying a
    cross-origin referer is `cross-site`. Claiming `none` while sending a
    `Referer` is self-contradictory, which is worse than sending neither.
    """

    values: dict[str, str] = {
        "user-agent": user_agent,
        "accept": _NAVIGATION_ACCEPT,
        "accept-language": accept_language,
        "accept-encoding": accept_encoding,
        "upgrade-insecure-requests": "1",
        "sec-fetch-site": "cross-site" if referer else "none",
        "sec-fetch-mode": "navigate",
        "sec-fetch-user": "?1",
        "sec-fetch-dest": "document",
        # Chrome sends this on navigations as of the priority-hints rollout.
        "priority": "u=0, i",
        **{k.lower(): v for k, v in client_hints.items()},
    }
    if referer:
        values["referer"] = referer
    return order_headers(values)


def order_headers(values: dict[str, str]) -> dict[str, str]:
    """`values` reordered to Chrome's sequence, with any unrecognised header
    appended in its original relative order rather than dropped."""

    ordered = {name: values[name] for name in _ORDER if name in values}
    ordered.update({k: v for k, v in values.items() if k not in ordered})
    return ordered


def header_order(headers: Iterable[str]) -> list[str]:
    """The order `headers` would be emitted in -- for assertions."""

    return list(order_headers(dict.fromkeys(headers, "")))
