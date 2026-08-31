"""Attaching to a browser someone else is managing.

The second way the local object drives a remote browser, alongside the wire
`/execute` path -- and the cheaper one to build, because both halves already
existed and were simply never connected:

- agentpilot's `routes/cdp.py` already serves Chrome's own `/json/version`
  shape, rewriting `webSocketDebuggerUrl` to its own relay with a credential
  already embedded in the query string.
- `_resolve_cdp_url` already GETs `/json/version` and reads that field back.

The one thing missing was that the GET sent no headers, and the discovery route
is behind `Authorization: Bearer`. These tests pin the header actually arriving,
because that single hop is the whole of the gap.

`pytest-httpserver` stands in for the gateway: what matters is the protocol
shape, and a real gateway needs Redis, Postgres and Chrome.
"""

from __future__ import annotations

import pytest
from pytest_httpserver import HTTPServer

from crawlpilot.driver.patchright_driver import _resolve_cdp_url
from crawlpilot.spi.errors import ContextCrashed

WS = "ws://node-7.internal:9222/devtools/browser/abc123"


def _serve_version(httpserver: HTTPServer, ws_url: str = WS) -> None:
    """What agentpilot answers with -- Chrome's payload, `webSocketDebuggerUrl`
    rewritten to the gateway's own relay."""

    httpserver.expect_request("/v1/sessions/s-1/cdp/json/version").respond_with_json(
        {
            "Browser": "Chrome/140.0.0.0",
            "Protocol-Version": "1.3",
            "webSocketDebuggerUrl": ws_url,
        }
    )


async def test_the_discovery_request_carries_the_bearer_token(httpserver: HTTPServer) -> None:
    """The gap this closes. Without the header the gateway answers 401 and the
    attach fails with a message about the browser being unreachable -- which
    sends you looking at Chrome rather than at your credential."""

    httpserver.expect_request(
        "/v1/sessions/s-1/cdp/json/version",
        headers={"Authorization": "Bearer secret-key"},
    ).respond_with_json({"webSocketDebuggerUrl": WS})

    resolved = await _resolve_cdp_url(
        httpserver.url_for("/v1/sessions/s-1/cdp/json/version"),
        {"Authorization": "Bearer secret-key"},
    )
    assert resolved == WS


async def test_a_plain_endpoint_still_needs_no_headers(httpserver: HTTPServer) -> None:
    """A bare `--remote-debugging-port` is the common case and must stay a
    one-argument attach."""

    _serve_version(httpserver)
    resolved = await _resolve_cdp_url(httpserver.url_for("/v1/sessions/s-1/cdp/json/version"))
    assert resolved == WS


async def test_the_json_version_suffix_is_optional(httpserver: HTTPServer) -> None:
    """Chrome's own convention: callers pass the base and the path is appended.
    A caller who pasted the full discovery URL must not get it twice."""

    _serve_version(httpserver)
    for url in (
        httpserver.url_for("/v1/sessions/s-1/cdp/json/version"),
        httpserver.url_for("/v1/sessions/s-1/cdp"),
        httpserver.url_for("/v1/sessions/s-1/cdp") + "/",
    ):
        assert await _resolve_cdp_url(url) == WS


async def test_a_websocket_url_is_passed_through_untouched() -> None:
    """No discovery hop, so no headers are involved -- and none are needed: the
    gateway's WS leg authenticates by query string, since a browser cannot set
    headers on a handshake."""

    assert await _resolve_cdp_url("wss://gw/v1/sessions/s-1/cdp?api_key=k") == (
        "wss://gw/v1/sessions/s-1/cdp?api_key=k"
    )


async def test_an_unreachable_endpoint_names_the_url(httpserver: HTTPServer) -> None:
    with pytest.raises(ContextCrashed, match="no browser answering"):
        await _resolve_cdp_url(httpserver.url_for("/nothing-here"))


async def test_a_response_without_the_websocket_field_is_rejected(
    httpserver: HTTPServer,
) -> None:
    """Something answered, but it was not a CDP endpoint. Failing here beats
    handing `connect_over_cdp` an empty string."""

    httpserver.expect_request("/json/version").respond_with_json({"Browser": "not-chrome"})
    with pytest.raises(ContextCrashed, match="no webSocketDebuggerUrl"):
        await _resolve_cdp_url(httpserver.url_for("/"))


# ------------------------------------------------------------------ plumbing


def test_the_facades_carry_headers_down_to_the_launch_config() -> None:
    """A guard on the wiring rather than the behaviour: `cdp_headers` is only
    useful if it survives the trip from the argument a caller types to the
    `LaunchConfig` the driver reads, and every hop is an easy one to forget."""

    from crawlpilot import Browser

    browser = Browser(
        cdp_url="http://gw/v1/sessions/s-1/cdp/json/version",
        cdp_headers={"Authorization": "Bearer k"},
    )
    assert browser.config.launch.cdp_headers == {"Authorization": "Bearer k"}
    assert browser.config.launch.cdp_url == "http://gw/v1/sessions/s-1/cdp/json/version"


def test_headers_default_to_none_so_a_plain_endpoint_is_unaffected() -> None:
    from crawlpilot import Browser

    assert Browser().config.launch.cdp_headers is None
