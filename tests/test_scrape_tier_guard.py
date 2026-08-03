"""`routes/scrape.py`'s fail-closed proxy guard (anti-detection A2) -- a
pure logic check that runs before any browser/driver use, so no real
Patchright context is needed. `tier=stealth|enhanced` with no proxy pool
configured must 503 with a descriptive error rather than silently scraping
from the raw host IP; `basic`/`auto` must stay lenient."""

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


async def test_stealth_tier_without_proxy_pool_fails_closed() -> None:
    with pytest.raises(HTTPException) as exc_info:
        await scrape(
            ScrapeRequest(tenant="acme", url="https://www.zara.com/", tier="stealth"),
            _FakeRequest(),
            _NoProxyWiring(),  # type: ignore[arg-type]
        )
    assert exc_info.value.status_code == 503
    assert "AGENTPILOT_PROXY_POOL" in exc_info.value.detail


async def test_enhanced_tier_without_proxy_pool_fails_closed() -> None:
    with pytest.raises(HTTPException) as exc_info:
        await scrape(
            ScrapeRequest(tenant="acme", url="https://www.zara.com/", tier="enhanced"),
            _FakeRequest(),
            _NoProxyWiring(),  # type: ignore[arg-type]
        )
    assert exc_info.value.status_code == 503


async def test_basic_tier_without_proxy_pool_is_not_blocked_by_the_guard() -> None:
    # The guard must NOT fire for basic/auto -- it would then fall through to
    # real scraping (which needs a browser), so we only assert that IF it
    # raises, it is not our 503 guard. A bad URL/domain check or driver
    # access is out of scope here.
    try:
        await scrape(
            ScrapeRequest(tenant="acme", url="https://www.zara.com/", tier="basic"),
            _FakeRequest(),
            _NoProxyWiring(),  # type: ignore[arg-type]
        )
    except HTTPException as exc:
        assert not (exc.status_code == 503 and "AGENTPILOT_PROXY_POOL" in str(exc.detail))
    except Exception:
        # Any non-HTTPException (e.g. AttributeError reaching for a driver the
        # fake wiring lacks) means the guard correctly let it proceed past.
        pass
