"""The client, against a stand-in gateway.

`pytest-httpserver` rather than a live gateway: a real one needs Redis,
Postgres and Chrome, and what these tests are about is the protocol -- what goes
on the wire, what comes back, and that the verbs a caller writes are the ones a
local browser would have taken.

Runnable with only `agentpilot-client` installed, which is the property CI's
clean-venv job asserts. Nothing here imports `agentpilot`.
"""

from __future__ import annotations

import base64
import json

import pytest
from pytest_httpserver import HTTPServer

from agentpilot_client import AgentPilot, AsyncAgentPilot, IncompatibleServer, Transport
from crawlpilot.spi.errors import CapacityExhausted, DriverError, StaleRefError
from crawlpilot.verbs import SessionVerbs
from crawlpilot.wire import WIRE_API_VERSION

KEY = "test-key"


def _transport(httpserver: HTTPServer) -> Transport:
    return Transport(httpserver.url_for("/"), KEY, timeout=5.0, max_retries=1)


def _capabilities(httpserver: HTTPServer, **overrides) -> None:  # type: ignore[no-untyped-def]
    httpserver.expect_request("/v1/capabilities").respond_with_json(
        {
            "wire_api": WIRE_API_VERSION,
            "crawlpilot_version": "0.2.0",
            "role": "worker",
            "tools": [],
            "extensions": [],
            **overrides,
        }
    )


# ----------------------------------------------------------------- transport


async def test_every_request_carries_the_bearer_token(httpserver: HTTPServer) -> None:
    httpserver.expect_request(
        "/v1/scrape", headers={"Authorization": f"Bearer {KEY}"}
    ).respond_with_json(
        {"success": True, "data": {"document_id": "d1", "url": "u", "markdown": "#"}}
    )

    async with AsyncAgentPilot(transport=_transport(httpserver)) as ap:
        assert (await ap.scrape("https://example.com")).markdown == "#"


async def test_a_server_error_becomes_the_exception_it_names(httpserver: HTTPServer) -> None:
    """The envelope is decoded through `crawlpilot.spi.errors`, so the `except`
    clauses written against a local browser catch the remote one too -- and
    neither side keeps a code->class table."""

    httpserver.expect_request("/v1/scrape").respond_with_json(
        {"success": False, "code": "STALE_REF", "error": "ref 'e12' is not available"},
        status=409,
    )

    async with AsyncAgentPilot(transport=_transport(httpserver)) as ap:
        with pytest.raises(StaleRefError, match="not available"):
            await ap.scrape("https://example.com")


async def test_an_unknown_code_is_still_catchable(httpserver: HTTPServer) -> None:
    """A client one release behind its server must not crash on a code it has
    never seen."""

    httpserver.expect_request("/v1/scrape").respond_with_json(
        {"success": False, "code": "FROM_A_LATER_VERSION", "error": "something new"},
        status=400,
    )

    async with AsyncAgentPilot(transport=_transport(httpserver)) as ap:
        with pytest.raises(DriverError, match="something new"):
            await ap.scrape("https://example.com")


async def test_capacity_exhausted_is_retried_then_raised(httpserver: HTTPServer) -> None:
    """`Retry-After` is honoured on the two statuses that set it. After the
    retries are spent the error is raised rather than swallowed."""

    httpserver.expect_request("/v1/scrape").respond_with_json(
        {"success": False, "code": "CAPACITY_EXHAUSTED", "error": "fleet full"},
        status=503,
        headers={"Retry-After": "0"},
    )

    async with AsyncAgentPilot(transport=_transport(httpserver)) as ap:
        with pytest.raises(CapacityExhausted, match="fleet full"):
            await ap.scrape("https://example.com")


# -------------------------------------------------------------- handshake


async def test_a_newer_server_is_refused_with_both_versions_named(
    httpserver: HTTPServer,
) -> None:
    """Refused up front rather than failing deep inside a later call with a
    schema rejection that names a field instead of the real problem."""

    _capabilities(httpserver, wire_api="99.0")

    async with AsyncAgentPilot(transport=_transport(httpserver)) as ap:
        with pytest.raises(IncompatibleServer) as caught:
            await ap.capabilities()
    assert "99.0" in str(caught.value)
    assert WIRE_API_VERSION in str(caught.value)


async def test_an_older_server_is_accepted(httpserver: HTTPServer) -> None:
    """It simply lacks verbs we know about, which is not a reason to refuse."""

    _capabilities(httpserver, wire_api="0.9")
    async with AsyncAgentPilot(transport=_transport(httpserver)) as ap:
        assert (await ap.capabilities())["wire_api"] == "0.9"


async def test_an_extension_verb_the_server_reports_becomes_callable(
    httpserver: HTTPServer,
) -> None:
    """The client's own `CATALOG` has never heard of `walmart.solve_wall`. It is
    callable anyway, because the registry is built from what the *server* said --
    which is the whole reason `/v1/capabilities` exists."""

    _capabilities(
        httpserver,
        tools=[
            {
                "name": "walmart.solve_wall",
                "namespace": "walmart",
                "description": "Clear a known wall.",
                "safety": "safe",
            }
        ],
    )

    async with AsyncAgentPilot(transport=_transport(httpserver)) as ap:
        registry = await ap.tools()
    assert "walmart.solve_wall" in registry.names


# ---------------------------------------------------------------- session


def _session_endpoints(httpserver: HTTPServer, result: dict) -> None:  # type: ignore[type-arg]
    _capabilities(httpserver)
    httpserver.expect_request("/v1/sessions", method="POST").respond_with_json(
        {"session_id": "s-1", "metadata": {"tier_used": "stealth", "node_id": "node-7",
                                           "duration_ms": 12.0}}
    )
    httpserver.expect_request("/v1/sessions/s-1/execute", method="POST").respond_with_json(result)
    httpserver.expect_request("/v1/sessions/s-1", method="DELETE").respond_with_data("")


async def test_a_remote_session_is_a_sessionverbs(httpserver: HTTPServer) -> None:
    """The headline. Not "has similar methods" -- *is* the class the local
    session is, so a script written against one runs against the other."""

    _session_endpoints(httpserver, {"extracts": [], "values": []})

    async with AsyncAgentPilot(transport=_transport(httpserver)) as ap:
        async with ap.session(domain="example.com") as page:
            assert isinstance(page, SessionVerbs)
            assert page.session_id == "s-1"
            assert page.tier == "stealth"
            assert page.node_id == "node-7"


async def test_a_typed_getter_reads_values_not_prose(httpserver: HTTPServer) -> None:
    """`values` is the field the hand-written wire model used to drop, which is
    what made these verbs impossible remotely: with only `readouts`,
    `is_visible()` returned the *sentence* `"#buy is not visible"` -- non-empty,
    therefore truthy."""

    _session_endpoints(httpserver, {"values": [False], "readouts": ["#buy is not visible"]})

    async with AsyncAgentPilot(transport=_transport(httpserver)) as ap:
        async with ap.session() as page:
            assert await page.is_visible(selector="#buy") is False


async def test_the_action_a_verb_sends_is_the_shape_the_gateway_validates(
    httpserver: HTTPServer,
) -> None:
    """Both sides project from the same `ToolSpec`, so this asserts the payload
    rather than a translation."""

    _capabilities(httpserver)
    httpserver.expect_request("/v1/sessions", method="POST").respond_with_json(
        {"session_id": "s-1", "metadata": {}}
    )
    seen: list[dict] = []  # type: ignore[type-arg]

    def _record(request):  # type: ignore[no-untyped-def]
        from werkzeug.wrappers import Response

        seen.append(json.loads(request.get_data()))
        return Response(json.dumps({"extracts": []}), content_type="application/json")

    httpserver.expect_request("/v1/sessions/s-1/execute").respond_with_handler(_record)
    httpserver.expect_request("/v1/sessions/s-1", method="DELETE").respond_with_data("")

    async with AsyncAgentPilot(transport=_transport(httpserver)) as ap:
        async with ap.session() as page:
            await page.navigate("https://example.com")
            await page.click(ref="e12")

    assert seen[0]["actions"][0]["type"] == "navigate"
    assert seen[0]["actions"][0]["url"] == "https://example.com"
    assert seen[1]["actions"][0] == {"type": "click", "ref": "e12", "all": False}


async def test_a_snapshot_comes_back_as_a_snapshot(httpserver: HTTPServer) -> None:
    """The one deliberate asymmetry: the fused tree stays local, the serialized
    view crosses the network -- and `snapshot()` returns the latter on both."""

    _session_endpoints(
        httpserver,
        {"snapshots": [{"llm_text": '[e1]<button "Buy"/>',
                        "refs": {"e1": {"role": "button", "name": "Buy", "bbox": None}}}]},
    )

    async with AsyncAgentPilot(transport=_transport(httpserver)) as ap:
        async with ap.session() as page:
            snapshot = await page.snapshot()
    assert snapshot is not None
    assert snapshot.refs["e1"].name == "Buy"


async def test_screenshots_come_back_as_bytes(httpserver: HTTPServer) -> None:
    _session_endpoints(
        httpserver, {"screenshots": [base64.b64encode(b"\x89PNG").decode("ascii")]}
    )

    async with AsyncAgentPilot(transport=_transport(httpserver)) as ap:
        async with ap.session() as page:
            assert await page.screenshot() == b"\x89PNG"


async def test_the_session_is_released_even_when_the_body_raises(
    httpserver: HTTPServer,
) -> None:
    """A leaked session holds a lease until the reaper takes it, which on a busy
    fleet is capacity someone else is waiting for."""

    _session_endpoints(httpserver, {"extracts": []})

    async with AsyncAgentPilot(transport=_transport(httpserver)) as ap:
        with pytest.raises(RuntimeError):
            async with ap.session():
                raise RuntimeError("boom")

    assert any(
        log[0].path == "/v1/sessions/s-1" and log[0].method == "DELETE"
        for log in httpserver.log
    )


async def test_cdp_details_are_offered_for_the_escape_hatch(httpserver: HTTPServer) -> None:
    _session_endpoints(httpserver, {"extracts": []})

    async with AsyncAgentPilot(transport=_transport(httpserver)) as ap:
        async with ap.session(enable_cdp=True) as page:
            assert page.cdp_url().endswith("/v1/sessions/s-1/cdp/json/version")
            assert page.cdp_headers() == {"Authorization": f"Bearer {KEY}"}
            assert "api_key=" in page.live_view_url()
            assert page.live_view_url().startswith("ws")


# ------------------------------------------------------------------- scrape


async def test_scrape_returns_crawlpilots_own_document_type(
    httpserver: HTTPServer,
) -> None:
    """Not a client-defined mirror of it. A caller reading `.markdown` or
    `.metadata.tier_used` cannot tell which client produced the result."""

    httpserver.expect_request("/v1/scrape").respond_with_json(
        {
            "success": True,
            "data": {
                "document_id": "d1",
                "url": "https://example.com",
                "markdown": "# Example",
                "links": ["https://example.com/a"],
                "metadata": {
                    "title": "Example",
                    "status_code": 200,
                    "tier_used": "basic",
                    "node_id": "node-7",
                    "duration_ms": 42.0,
                    "source_url": "https://example.com",
                },
            },
        }
    )

    async with AsyncAgentPilot(transport=_transport(httpserver)) as ap:
        doc = await ap.scrape("https://example.com")

    assert doc.markdown == "# Example"
    assert doc.links == ("https://example.com/a",)
    assert doc.metadata is not None
    assert doc.metadata.tier_used == "basic"


async def test_the_client_never_sends_a_caller_supplied_tenant(
    httpserver: HTTPServer,
) -> None:
    """Every route overwrites `tenant` from the authenticated key, so the client
    offers no way to set one -- doing so would imply a caller could act for a
    tenant that is not theirs."""

    import inspect

    assert "tenant" not in inspect.signature(AsyncAgentPilot.__init__).parameters
    assert "tenant" not in inspect.signature(AsyncAgentPilot.scrape).parameters


async def test_batch_scrape_carries_failures_instead_of_losing_the_batch(
    httpserver: HTTPServer,
) -> None:
    """A fifty-URL run should not lose forty-nine good results to one dead
    host -- the same contract `Crawlpilot.batch_scrape` has."""

    httpserver.expect_ordered_request("/v1/scrape").respond_with_json(
        {"success": True, "data": {"document_id": "d1", "url": "a", "markdown": "ok"}}
    )
    httpserver.expect_ordered_request("/v1/scrape").respond_with_json(
        {"success": False, "code": "NAVIGATION_TIMEOUT", "error": "timed out"}, status=504
    )

    async with AsyncAgentPilot(transport=_transport(httpserver)) as ap:
        docs = await ap.batch_scrape(["https://a.test", "https://b.test"], concurrency=1)

    assert docs[0].markdown == "ok"
    assert docs[1].error is not None and "timed out" in docs[1].error


# -------------------------------------------------------------------- sync


def test_the_sync_client_is_the_async_one_without_await(httpserver: HTTPServer) -> None:
    """`AgentPilot` runs `AsyncAgentPilot` on a private loop thread, reusing
    `crawlpilot._sync` rather than a second copy of that machinery."""

    _session_endpoints(httpserver, {"values": ["Example Domain"]})
    httpserver.expect_request("/v1/scrape").respond_with_json(
        {"success": True, "data": {"document_id": "d1", "url": "u", "markdown": "# hi"}}
    )

    with AgentPilot(transport=_transport(httpserver)) as ap:
        assert ap.scrape("https://example.com").markdown == "# hi"
        with ap.session() as page:
            assert page.get_title() == "Example Domain"
            assert page.session_id == "s-1"
