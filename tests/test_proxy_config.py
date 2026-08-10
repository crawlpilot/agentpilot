"""Unit tests for `agentpilot.identity.proxy_config.ProxyConfig` -- the
client-configurable, tier-aware proxy resolver. Pure, no Redis/browser."""

from __future__ import annotations

import json

import pytest

from agentpilot.identity.proxy_config import ProxyConfig
from agentpilot.spi.proxy import ProxyEndpoint


def test_from_flat_is_the_default_pool_for_every_tenant_and_tier() -> None:
    ep = ProxyEndpoint(scheme="http", host="p", port=8080)
    cfg = ProxyConfig.from_flat([ep])
    assert cfg.is_empty is False
    assert cfg.resolve("anyone", "residential") == [ep]
    assert cfg.resolve("anyone", None) == [ep]


def test_empty_config_is_empty() -> None:
    assert ProxyConfig({}).is_empty is True
    assert ProxyConfig.from_flat([]).is_empty is True


def test_resolve_prefers_most_specific_then_falls_back() -> None:
    acme_res = ProxyEndpoint(scheme="http", host="acme-res", port=1, tier="residential")
    any_res = ProxyEndpoint(scheme="http", host="any-res", port=2, tier="residential")
    default = ProxyEndpoint(scheme="http", host="default", port=3)
    cfg = ProxyConfig(
        {
            ("acme", "residential"): (acme_res,),
            ("*", "residential"): (any_res,),
            ("*", "*"): (default,),
        }
    )
    # (tenant, tier) exact match wins.
    assert cfg.resolve("acme", "residential") == [acme_res]
    # Another tenant falls back to the (*, residential) pool.
    assert cfg.resolve("globex", "residential") == [any_res]
    # An unknown tier falls back to (*, *).
    assert cfg.resolve("acme", "datacenter") == [default]


def test_resolve_never_empty_while_any_pool_exists() -> None:
    ep = ProxyEndpoint(scheme="http", host="only", port=1, tier="mobile")
    cfg = ProxyConfig({("acme", "mobile"): (ep,)})
    # No matching wildcard pools, but a request still resolves to *some* endpoint.
    assert cfg.resolve("globex", "residential") == [ep]


def test_from_env_parses_flat_and_structured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTPILOT_PROXY_POOL", "http://u:p@flat-host:8000")
    monkeypatch.setenv(
        "AGENTPILOT_PROXY_POOLS",
        json.dumps(
            {
                "acme": {
                    "residential": ["http://ru:rp@acme-res:8000?country=in"],
                    "datacenter": ["http://acme-dc:8080"],
                }
            }
        ),
    )
    cfg = ProxyConfig.from_env()

    flat = cfg.resolve("globex", None)  # only the flat default matches
    assert flat[0].host == "flat-host" and flat[0].username == "u"

    res = cfg.resolve("acme", "residential")
    assert res[0].host == "acme-res"
    assert res[0].tier == "residential"
    assert res[0].country == "IN"  # normalized upper-case from ?country=in

    dc = cfg.resolve("acme", "datacenter")
    assert dc[0].host == "acme-dc" and dc[0].port == 8080


def test_from_env_empty_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENTPILOT_PROXY_POOL", raising=False)
    monkeypatch.delenv("AGENTPILOT_PROXY_POOLS", raising=False)
    assert ProxyConfig.from_env().is_empty is True
