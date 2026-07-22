"""`baas.egress.httpx_guard` -- post-DNS IP validation for the basic tier.
DNS resolution is monkeypatched for the blocked-IP cases so these don't
depend on real network/DNS; the allowed-path test hits a real local
`pytest-httpserver` instance (loopback isn't in the blocked-range list --
same scope as `baas.egress.policy`'s browser-process baseline)."""

from __future__ import annotations

import pytest
from pytest_httpserver import HTTPServer

from baas.egress import httpx_guard
from baas.spi.egress import EgressPolicy
from baas.spi.errors import EgressBlocked

DEFAULT_POLICY = EgressPolicy()


def test_metadata_ip_is_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx_guard, "resolve_all_ips", lambda host: ["169.254.169.254"])
    with pytest.raises(EgressBlocked):
        httpx_guard.assert_host_allowed("metadata.internal", DEFAULT_POLICY)


def test_rfc1918_ip_is_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx_guard, "resolve_all_ips", lambda host: ["10.0.0.5"])
    with pytest.raises(EgressBlocked):
        httpx_guard.assert_host_allowed("internal.corp", DEFAULT_POLICY)


def test_public_ip_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx_guard, "resolve_all_ips", lambda host: ["93.184.216.34"])
    httpx_guard.assert_host_allowed("example.com", DEFAULT_POLICY)  # no raise


def test_mixed_public_and_blocked_resolution_is_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    """DNS-rebinding shape: a hostname that resolves to *both* a public and
    a metadata IP must not be allowed just because one answer looks fine."""

    monkeypatch.setattr(
        httpx_guard, "resolve_all_ips", lambda host: ["93.184.216.34", "169.254.169.254"]
    )
    with pytest.raises(EgressBlocked):
        httpx_guard.assert_host_allowed("rebinding.example", DEFAULT_POLICY)


async def test_guarded_get_reaches_an_allowed_host(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/").respond_with_data("ok", content_type="text/plain")
    resp = await httpx_guard.guarded_get(httpserver.url_for("/"), DEFAULT_POLICY)
    assert resp.status_code == 200
    assert resp.text == "ok"


async def test_guarded_get_raises_before_connecting_to_blocked_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(httpx_guard, "resolve_all_ips", lambda host: ["169.254.169.254"])
    with pytest.raises(EgressBlocked):
        await httpx_guard.guarded_get("http://metadata.internal/latest/", DEFAULT_POLICY)
