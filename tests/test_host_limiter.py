"""`agentpilot.jobs.limiter` -- the pacing interval, the in-process gate, and
the backoff curve.

`RedisHostLimiter` runs its decision inside `acquire_host_slot.lua`, so it is
tested against `fakeredis` (which executes Lua) rather than mocked -- the whole
point of that script is atomicity, and a mock cannot tell you whether it holds.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from agentpilot.jobs.limiter import (
    MAX_BACKOFF_MULTIPLIER,
    InProcessHostLimiter,
    RedisHostLimiter,
    effective_interval_ms,
)

# --------------------------------------------------------- the interval itself


def test_the_effective_interval_is_the_larger_of_the_two() -> None:
    assert effective_interval_ms(delay_ms=500, robots_crawl_delay_seconds=2.0) == 2000
    assert effective_interval_ms(delay_ms=5000, robots_crawl_delay_seconds=2.0) == 5000


def test_a_caller_cannot_undercut_a_published_crawl_delay() -> None:
    """The point of taking the max rather than the caller's value: `Crawl-delay`
    is the host stating a rate it will serve, and `delay_ms=0` must not be a way
    to instruct this service to ignore it."""

    assert effective_interval_ms(delay_ms=0, robots_crawl_delay_seconds=1.0) == 1000


def test_no_delay_anywhere_means_no_pacing() -> None:
    assert effective_interval_ms(delay_ms=None, robots_crawl_delay_seconds=None) == 0


def test_a_negative_caller_delay_is_clamped_not_subtracted() -> None:
    assert effective_interval_ms(delay_ms=-5000, robots_crawl_delay_seconds=1.0) == 1000


# ------------------------------------------------------------- in-process gate


async def test_the_first_request_to_a_host_does_not_wait() -> None:
    limiter = InProcessHostLimiter()
    started = time.monotonic()
    await limiter.acquire("example.com", interval_ms=200)
    assert (time.monotonic() - started) < 0.1


async def test_consecutive_requests_to_one_host_are_spaced() -> None:
    limiter = InProcessHostLimiter()
    started = time.monotonic()
    await limiter.acquire("example.com", interval_ms=120)
    await limiter.acquire("example.com", interval_ms=120)
    elapsed = time.monotonic() - started
    # One interval, minus the jitter floor (-15%).
    assert elapsed >= 0.10


async def test_different_hosts_do_not_block_each_other() -> None:
    """Pacing is per host. A slow host must not hold up a crawl's other
    hosts -- that would turn one polite delay into a global one."""

    limiter = InProcessHostLimiter()
    started = time.monotonic()
    await asyncio.gather(
        limiter.acquire("a.example.com", interval_ms=500),
        limiter.acquire("b.example.com", interval_ms=500),
    )
    assert (time.monotonic() - started) < 0.2


async def test_a_zero_interval_is_a_no_op() -> None:
    limiter = InProcessHostLimiter()
    started = time.monotonic()
    for _ in range(50):
        await limiter.acquire("example.com", interval_ms=0)
    assert (time.monotonic() - started) < 0.1


# ----------------------------------------------------------------- backoff


def test_penalize_doubles_and_caps() -> None:
    limiter = InProcessHostLimiter()
    for _ in range(20):
        limiter.penalize("example.com")
    assert limiter._backoff.of("example.com") == MAX_BACKOFF_MULTIPLIER


def test_the_cap_exists_so_a_failing_host_cannot_stall_a_job_forever() -> None:
    """Without a ceiling a persistently-503ing host pushes its interval toward
    infinity: the job stops progressing without ever failing, which is the least
    debuggable outcome available."""

    assert MAX_BACKOFF_MULTIPLIER < float("inf")


def test_reward_halves_rather_than_clearing() -> None:
    """A host recovering from overload serves one request and fails the next.
    Dropping straight back to the base interval walks into the same wall."""

    limiter = InProcessHostLimiter()
    limiter.penalize("example.com")
    limiter.penalize("example.com")
    assert limiter._backoff.of("example.com") == 4.0
    limiter.reward("example.com")
    assert limiter._backoff.of("example.com") == 2.0
    limiter.reward("example.com")
    assert limiter._backoff.of("example.com") == 1.0


def test_reward_on_an_unpenalized_host_is_harmless() -> None:
    limiter = InProcessHostLimiter()
    limiter.reward("example.com")
    assert limiter._backoff.of("example.com") == 1.0


async def test_backoff_actually_lengthens_the_wait() -> None:
    limiter = InProcessHostLimiter()
    await limiter.acquire("example.com", interval_ms=100)
    limiter.penalize("example.com")  # x2
    limiter.penalize("example.com")  # x4
    started = time.monotonic()
    await limiter.acquire("example.com", interval_ms=100)
    # Base 100ms was already reserved by the first call; this one waits out
    # roughly that, and the *next* reservation is pushed 400ms out.
    assert (time.monotonic() - started) >= 0.05


# ------------------------------------------------------------- redis variant


@pytest.fixture
def fake_redis() -> object:
    fakeredis = pytest.importorskip("fakeredis")
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


async def test_redis_limiter_returns_zero_wait_for_an_idle_host(fake_redis: object) -> None:
    limiter = RedisHostLimiter(fake_redis)
    started = time.monotonic()
    await limiter.acquire("example.com", interval_ms=200)
    assert (time.monotonic() - started) < 0.1


async def test_redis_limiter_serializes_two_independent_workers(fake_redis: object) -> None:
    """The reason this is Lua and not a GET/SET pair in Python.

    Two limiter instances stand in for two worker processes. If the read and the
    reservation were separate steps, both would read the same "next allowed"
    value, both would conclude they may start now, and the host would see two
    requests where the crawl-delay said one -- an error that grows with the
    worker count.
    """

    worker_a = RedisHostLimiter(fake_redis)
    worker_b = RedisHostLimiter(fake_redis)
    started = time.monotonic()
    await asyncio.gather(
        worker_a.acquire("example.com", interval_ms=150),
        worker_b.acquire("example.com", interval_ms=150),
    )
    # One of the two had to wait out an interval; the fleet paced like one
    # crawler rather than two.
    assert (time.monotonic() - started) >= 0.12


async def test_redis_limiter_paces_locally_when_redis_is_broken() -> None:
    """Fails open on pacing, not closed. A Redis outage must not stall every
    crawl in the fleet -- under-pacing for the duration is the lesser harm, and
    it is logged rather than silent."""

    class _BrokenRedis:
        def register_script(self, script: str) -> object:
            async def run(**kwargs: object) -> int:
                raise RuntimeError("redis is down")

            return run

    limiter = RedisHostLimiter(_BrokenRedis())
    started = time.monotonic()
    await limiter.acquire("example.com", interval_ms=100)
    elapsed = time.monotonic() - started
    assert 0.05 <= elapsed < 1.0
