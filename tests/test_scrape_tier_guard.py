"""`routes/scrape.py`'s proxy guard -- a pure logic check that runs before any
browser/driver use, so no real Patchright context is needed.

The guard is opt-in (`AGENTPILOT_REQUIRE_PROXY_FOR_STEALTH`). It used to fail
closed by default on the assumption that a worker's own IP is a datacenter IP;
from a residential worker with no proxy, cos.com (Akamai) served the product
page 5/5 on the stealth tier, so the default blocked the configuration that
works."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from agentpilot.gateway.routes.scrape import scrape
from agentpilot.gateway.schemas import ScrapeRequest


class _FakeHeaders:
    def get(self, _key: str) -> str | None:
        return None


class _FakeRequest:
    headers = _FakeHeaders()


class _NoProxyWiring:
    """Enough surface for the guard to run and short-circuit before the
    driver is ever touched -- `proxy_pinner is None` is the condition under
    test."""

    proxy_pinner = None


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default-behaviour tests must not inherit either switch from the
    developer's shell."""

    monkeypatch.delenv("AGENTPILOT_ALLOW_PROXYLESS_STEALTH", raising=False)
    monkeypatch.delenv("AGENTPILOT_REQUIRE_PROXY_FOR_STEALTH", raising=False)


async def _scrape(tier: str) -> None:
    await scrape(
        ScrapeRequest(tenant="acme", url="https://www.zara.com/", tier=tier),
        _FakeRequest(),
        _NoProxyWiring(),  # type: ignore[arg-type]
    )


@pytest.mark.parametrize("tier", ["stealth", "enhanced"])
async def test_proxyless_stealth_runs_by_default(tier: str) -> None:
    # Past the guard, the request proceeds into the driver -- which this fake
    # wiring has none of. An AttributeError here means the 503 did NOT fire,
    # which is exactly what is under test.
    with pytest.raises((AttributeError, TypeError)):
        await _scrape(tier)


@pytest.mark.parametrize("tier", ["stealth", "enhanced"])
async def test_a_deployment_can_require_a_proxy(
    monkeypatch: pytest.MonkeyPatch, tier: str
) -> None:
    """For workers on a cloud ASN, where the IP really is the block signal."""

    monkeypatch.setenv("AGENTPILOT_REQUIRE_PROXY_FOR_STEALTH", "1")
    with pytest.raises(HTTPException) as exc_info:
        await _scrape(tier)
    assert exc_info.value.status_code == 503
    assert "AGENTPILOT_PROXY_POOL" in exc_info.value.detail


async def test_the_old_opt_out_still_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """An existing deployment that set the old opt-out keeps its behaviour even
    if the new switch is also set."""

    monkeypatch.setenv("AGENTPILOT_REQUIRE_PROXY_FOR_STEALTH", "1")
    monkeypatch.setenv("AGENTPILOT_ALLOW_PROXYLESS_STEALTH", "true")
    with pytest.raises((AttributeError, TypeError)):
        await _scrape("stealth")


@pytest.mark.parametrize("tier", ["basic", "auto"])
async def test_basic_and_auto_are_never_gated(
    monkeypatch: pytest.MonkeyPatch, tier: str
) -> None:
    monkeypatch.setenv("AGENTPILOT_REQUIRE_PROXY_FOR_STEALTH", "1")
    try:
        await _scrape(tier)
    except HTTPException as exc:
        assert not (exc.status_code == 503 and "AGENTPILOT_PROXY_POOL" in str(exc.detail))
    except Exception:
        # Any non-HTTPException (e.g. AttributeError reaching for a driver the
        # fake wiring lacks) means the guard correctly let it proceed past.
        pass
