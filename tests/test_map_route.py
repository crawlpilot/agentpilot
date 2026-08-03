"""`routes/map.py`'s `map_urls()` -- unit-level, no HTTP layer (this repo's
existing route-testing convention, see `test_sessions_list.py`): calls the
route function directly, bypassing FastAPI's `Depends()` resolution."""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from pytest_httpserver import HTTPServer

from agentpilot.auth.models import AuthedTenant
from agentpilot.gateway.routes.map import map_urls
from agentpilot.gateway.schemas import MapRequest


async def test_map_urls_overwrites_a_mismatched_request_tenant_with_the_authed_one(
    httpserver: HTTPServer,
) -> None:
    httpserver.expect_request("/").respond_with_data("<html></html>", content_type="text/html")
    httpserver.expect_request("/sitemap.xml").respond_with_data("", status=404)

    req = MapRequest(tenant="someone-else", url=httpserver.url_for("/"))
    authed = AuthedTenant(tenant="acme", key_id="k1")

    resp = await map_urls(req, authed)

    assert resp.success is True


async def test_map_urls_rejects_a_non_http_url() -> None:
    req = MapRequest(tenant="acme", url="ftp://example.com/")
    authed = AuthedTenant(tenant="acme", key_id="k1")

    with pytest.raises(HTTPException) as exc_info:
        await map_urls(req, authed)
    assert exc_info.value.status_code == 400


async def test_map_urls_returns_discovered_links(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/").respond_with_data(
        '<a href="/a">A</a>', content_type="text/html"
    )
    httpserver.expect_request("/sitemap.xml").respond_with_data("", status=404)

    req = MapRequest(tenant="acme", url=httpserver.url_for("/"))
    authed = AuthedTenant(tenant="acme", key_id="k1")

    resp = await map_urls(req, authed)

    assert [link.url for link in resp.links] == [httpserver.url_for("/a")]
