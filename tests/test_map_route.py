"""`routes/map.py`'s `map_urls()` -- unit-level, no HTTP layer (this repo's
existing route-testing convention, see `test_sessions_list.py`): calls the
route function directly, bypassing FastAPI's `Depends()` resolution."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException
from pytest_httpserver import HTTPServer

import agentpilot.gateway.routes.map as map_mod
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


async def test_map_urls_ranks_results_by_the_search_query(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/").respond_with_data(
        f'<a href="{httpserver.url_for("/about")}">x</a>'
        f'<a href="{httpserver.url_for("/pricing")}">y</a>',
        content_type="text/html",
    )
    httpserver.expect_request("/robots.txt").respond_with_data("nope", status=404)
    httpserver.expect_request("/sitemap.xml").respond_with_data("nope", status=404)

    req = MapRequest(tenant="acme", url=httpserver.url_for("/"), search="pricing")
    authed = AuthedTenant(tenant="acme", key_id="k1")

    resp = await map_urls(req, authed)

    assert resp.links[0].url == httpserver.url_for("/pricing")


async def test_map_urls_returns_408_when_discovery_exceeds_the_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _slow(options: object, policy: object) -> list:
        await asyncio.sleep(0.2)
        return []

    async def _noop_resolve(url: str, policy: object) -> str:
        return url

    monkeypatch.setattr(map_mod, "discover_for_map", _slow)
    monkeypatch.setattr(map_mod, "_resolve_seed_url", _noop_resolve)

    req = MapRequest(tenant="acme", url="https://example.com/", timeout=1)
    authed = AuthedTenant(tenant="acme", key_id="k1")

    with pytest.raises(HTTPException) as exc_info:
        await map_urls(req, authed)
    assert exc_info.value.status_code == 408


async def test_map_urls_warns_when_a_subpath_yields_no_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _empty(options: object, policy: object) -> list:
        return []

    async def _noop_resolve(url: str, policy: object) -> str:
        return url

    monkeypatch.setattr(map_mod, "discover_for_map", _empty)
    monkeypatch.setattr(map_mod, "_resolve_seed_url", _noop_resolve)

    req = MapRequest(tenant="acme", url="https://docs.example.com/guide")
    authed = AuthedTenant(tenant="acme", key_id="k1")

    resp = await map_urls(req, authed)

    assert resp.warning is not None
    assert "example.com" in resp.warning


async def test_map_urls_does_not_warn_on_a_base_domain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _empty(options: object, policy: object) -> list:
        return []

    async def _noop_resolve(url: str, policy: object) -> str:
        return url

    monkeypatch.setattr(map_mod, "discover_for_map", _empty)
    monkeypatch.setattr(map_mod, "_resolve_seed_url", _noop_resolve)

    req = MapRequest(tenant="acme", url="https://example.com/")
    authed = AuthedTenant(tenant="acme", key_id="k1")

    resp = await map_urls(req, authed)

    assert resp.warning is None
