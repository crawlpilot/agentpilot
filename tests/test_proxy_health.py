"""`agentpilot.identity.proxy_health.ProxyHealth` -- per-proxy retirement
(served-too-many-successes cap + 3-strike connection losses) over fakeredis."""

from __future__ import annotations

import fakeredis

from agentpilot.identity.proxy_health import ProxyHealth
from agentpilot.spi.proxy import ProxyEndpoint

PROXY = ProxyEndpoint(scheme="http", host="p", port=8080, tier="residential")
OTHER = ProxyEndpoint(scheme="http", host="q", port=8080, tier="residential")


def _health(max_success: int) -> ProxyHealth:
    # max_success=1 -> jitter span int(0.25)=0 -> cap is exactly 1 (deterministic).
    return ProxyHealth(fakeredis.aioredis.FakeRedis(), max_success=max_success)


async def test_not_retired_initially() -> None:
    assert await _health(100).is_retired(PROXY) is False


async def test_success_cap_retires() -> None:
    h = _health(1)  # cap == 1
    assert await h.record_success(PROXY) is True  # crosses the cap
    assert await h.is_retired(PROXY) is True
    # Retirement is per physical proxy, not global.
    assert await h.is_retired(OTHER) is False


async def test_three_losses_retire() -> None:
    h = _health(100)
    assert await h.record_loss(PROXY) is False
    assert await h.record_loss(PROXY) is False
    assert await h.record_loss(PROXY) is True  # third strike
    assert await h.is_retired(PROXY) is True


async def test_record_is_noop_after_retirement() -> None:
    h = _health(1)
    await h.record_success(PROXY)
    assert await h.record_success(PROXY) is True  # still retired, no error


async def test_reset_revives_a_proxy() -> None:
    h = _health(1)
    await h.record_success(PROXY)
    assert await h.is_retired(PROXY) is True
    await h.reset(PROXY)
    assert await h.is_retired(PROXY) is False


async def test_cap_is_stable_across_calls_with_jitter() -> None:
    # With a larger cap the jitter is non-zero but must stay within +/-25% and
    # not change between successes (it's fixed on first success).
    h = ProxyHealth(fakeredis.aioredis.FakeRedis(), max_success=100)
    for _ in range(50):
        await h.record_success(PROXY)
    assert await h.is_retired(PROXY) is False  # 50 << ~[75,125] cap
