"""Unit tests for `agentpilot.session.browser_headers`. Pure; no network.

The `basic` tier used to send five headers and nothing else. Every assertion
here is about a header whose *absence* is a one-line rule at a WAF edge, long
before anything as subtle as a TLS fingerprint is consulted.
"""

from __future__ import annotations

import importlib

from agentpilot.session import browser_headers as bh

HINTS = {
    "sec-ch-ua": '"Chromium";v="140", "Not=A?Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}


def _headers(**kw):
    return bh.navigation_headers(
        user_agent="Mozilla/5.0 ... Chrome/140.0.0.0 Safari/537.36",
        accept_language="en-GB,en;q=0.9",
        client_hints=HINTS,
        **kw,
    )


def test_every_header_chrome_sends_on_a_navigation_is_present() -> None:
    headers = _headers()

    for required in (
        "accept",
        "accept-encoding",
        "accept-language",
        "upgrade-insecure-requests",
        "sec-fetch-site",
        "sec-fetch-mode",
        "sec-fetch-user",
        "sec-fetch-dest",
        "user-agent",
        "sec-ch-ua",
    ):
        assert required in headers, f"missing {required}"


def test_header_order_matches_chrome() -> None:
    """Header order is itself a fingerprint, hashed alongside the ClientHello.
    A dict literal starting with User-Agent -- which is what this replaced --
    matches no browser."""

    order = list(_headers(referer="https://www.google.com/"))

    assert order.index("sec-ch-ua") < order.index("user-agent")
    assert order.index("user-agent") < order.index("accept")
    assert order.index("accept") < order.index("sec-fetch-site")
    assert order.index("sec-fetch-dest") < order.index("accept-encoding")
    assert order.index("accept-encoding") < order.index("accept-language")


def test_sec_fetch_site_agrees_with_the_referer() -> None:
    """Claiming `none` while sending a Referer is self-contradictory, which is
    worse than sending neither."""

    assert _headers()["sec-fetch-site"] == "none"
    assert "referer" not in _headers()

    with_ref = _headers(referer="https://www.google.com/")
    assert with_ref["sec-fetch-site"] == "cross-site"
    assert with_ref["referer"] == "https://www.google.com/"


def test_accept_encoding_only_claims_what_can_be_decoded() -> None:
    """Advertising an encoding the client cannot decode is worse than
    advertising a smaller set: the server honours the claim and the response is
    undecodable."""

    claimed = set(_headers()["accept-encoding"].replace(" ", "").split(","))

    assert {"gzip", "deflate"} <= claimed
    for module, coding in (("brotli", "br"), ("zstandard", "zstd")):
        try:
            importlib.import_module(module)
        except ImportError:
            assert coding not in claimed, f"claimed {coding} without {module}"
        else:
            assert coding in claimed, f"can decode {coding} but did not claim it"


def test_client_hints_are_lowercased_into_the_set() -> None:
    headers = bh.navigation_headers(
        user_agent="UA",
        accept_language="en",
        client_hints={"Sec-CH-UA-Platform": '"Linux"'},
    )

    assert headers["sec-ch-ua-platform"] == '"Linux"'


def test_unknown_headers_are_kept_not_dropped() -> None:
    ordered = bh.order_headers({"user-agent": "UA", "x-custom": "1"})

    assert ordered == {"user-agent": "UA", "x-custom": "1"}
