"""Per-host pacing for the crawl worker: how long to wait before starting the
next request against a given host, and how much to back off when that host
starts saying no.

Two things the crawler already knew but never acted on:

* `CrawlOptions.delay_ms` was parsed, stored in `jobs.options`, round-tripped
  by `options_codec` -- and read by nobody. A caller who asked for a 2-second
  delay got none.
* `crawl.robots.crawl_delay()` parsed `Crawl-delay:` out of robots.txt and had
  no caller either, so the worker was hammering hosts that had explicitly asked
  it not to.

Both are honoured here, at whichever is the larger of the two: a caller's own
delay is a floor, and a host's published `Crawl-delay` is not something a
caller's smaller number should be able to override.

**Why Redis and not a dict.** Every worker process polls the same
`crawl_tasks` table, so politeness enforced per process is politeness divided
by the number of processes -- three workers each faithfully honouring a
one-second delay produce three requests a second. Worse, that error grows
exactly when a deployment scales up, which is when it can least afford to look
like an attack. `RedisHostLimiter` keeps the decision in one place;
`InProcessHostLimiter` is the correct fallback for a single-worker deployment
and the seam the tests drive.

Adapted in spirit from crawl4ai's `RateLimiter` (Apache-2.0; `crawl4ai/
async_dispatcher.py`), which is per-process and in-memory -- the right shape for
a library embedded in one program, and the thing that had to change for a
service that runs several.
"""

from __future__ import annotations

import asyncio
import random
import time
from pathlib import Path
from typing import Any, Protocol

import structlog

log = structlog.get_logger(__name__)

_LUA_DIR = Path(__file__).resolve().parent / "lua"

RETRY_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
"""Statuses that mean "slow down" rather than "this page is broken". 429 and 503
say it outright; 500/502/504 from a host that was fine a moment ago are usually
that host shedding load, and treating them as a pacing signal costs a little
throughput and buys not being the reason it fell over."""

MAX_BACKOFF_MULTIPLIER = 32.0
"""Cap on how far a misbehaving host can stretch its own interval. Without a cap
a host that 503s persistently pushes its delay towards infinity and the job
never finishes -- it just stops making progress without ever failing, which is
the least debuggable outcome available."""

JITTER = 0.15
"""±15% on every computed wait. A worker fleet that all backs off by exactly the
same multiplier re-converges into synchronised bursts, which is the pattern the
backoff was supposed to break up."""


def effective_interval_ms(
    *, delay_ms: int | None, robots_crawl_delay_seconds: float | None
) -> int:
    """The larger of what the caller asked for and what the host published.

    Not the smaller, and not the caller's alone: `Crawl-delay` is the host
    stating a rate it is willing to serve, and a caller passing `delay_ms=0`
    should not be able to instruct this service to ignore it.
    """

    caller = max(delay_ms or 0, 0)
    robots = int((robots_crawl_delay_seconds or 0) * 1000)
    return max(caller, robots)


def _jittered(wait_ms: float) -> float:
    if wait_ms <= 0:
        return 0.0
    return wait_ms * (1.0 + random.uniform(-JITTER, JITTER))


class HostLimiter(Protocol):
    async def acquire(self, host: str, *, interval_ms: int) -> None: ...

    def penalize(self, host: str) -> None: ...

    def reward(self, host: str) -> None: ...


class _Backoff:
    """The multiplier half, which is process-local in both implementations.

    Deliberately not shared through Redis. A 503 is evidence about the host, but
    it reaches exactly one worker, and the pacing floor that *is* shared already
    keeps the fleet's aggregate rate correct. Sharing the multiplier too would
    mean one worker's bad luck throttling every other worker's unrelated tasks
    against the same host, and recovering from that needs consensus about when
    things got better -- a lot of machinery for a second-order effect.
    """

    def __init__(self) -> None:
        self._multipliers: dict[str, float] = {}

    def of(self, host: str) -> float:
        return self._multipliers.get(host, 1.0)

    def penalize(self, host: str) -> None:
        current = self._multipliers.get(host, 1.0)
        self._multipliers[host] = min(current * 2.0, MAX_BACKOFF_MULTIPLIER)
        log.info(
            "host_limiter.backoff_increased", host=host, multiplier=self._multipliers[host]
        )

    def reward(self, host: str) -> None:
        """Halve the penalty on a success rather than clearing it.

        A host recovering from overload will serve one request and then fail the
        next; dropping straight back to the base interval on that first success
        walks into the same wall again. Halving climbs down at the same rate it
        climbed up.
        """

        current = self._multipliers.get(host)
        if current is None or current <= 1.0:
            self._multipliers.pop(host, None)
            return
        self._multipliers[host] = max(current / 2.0, 1.0)


class InProcessHostLimiter:
    """Correct for one worker process; the fallback when Redis is unavailable.

    Under several workers this under-paces by roughly the worker count, which is
    why `RedisHostLimiter` exists and is preferred by the wiring. Named
    "in-process" rather than "default" so that reading a stack trace tells you
    which one you got.
    """

    def __init__(self) -> None:
        self._next_allowed: dict[str, float] = {}
        self._backoff = _Backoff()
        self._lock = asyncio.Lock()

    async def acquire(self, host: str, *, interval_ms: int) -> None:
        interval = interval_ms * self._backoff.of(host)
        if interval <= 0:
            return
        async with self._lock:
            now = time.monotonic() * 1000
            start_at = max(self._next_allowed.get(host, now), now)
            self._next_allowed[host] = start_at + interval
            wait_ms = start_at - now
        if wait_ms > 0:
            await asyncio.sleep(_jittered(wait_ms) / 1000)

    def penalize(self, host: str) -> None:
        self._backoff.penalize(host)

    def reward(self, host: str) -> None:
        self._backoff.reward(host)


class RedisHostLimiter:
    """The pacing floor in Redis, so N workers pace a host like one crawler.

    `acquire_host_slot.lua` does the read-and-reserve in one step; see that
    script's comment for why splitting it defeats the purpose.
    """

    def __init__(self, redis: Any, *, key_prefix: str = "hostpace") -> None:
        self._redis = redis
        self._prefix = key_prefix
        self._backoff = _Backoff()
        self._script = redis.register_script(
            (_LUA_DIR / "acquire_host_slot.lua").read_text()
        )

    async def acquire(self, host: str, *, interval_ms: int) -> None:
        interval = int(interval_ms * self._backoff.of(host))
        if interval <= 0:
            return
        # TTL generously past one interval: the key only needs to outlive the
        # gap between consecutive requests to the same host, and letting an idle
        # host's key expire keeps Redis from accumulating one per host crawled.
        ttl_seconds = max(int(interval / 1000) * 4, 60)
        try:
            wait_ms = await self._script(
                keys=[f"{self._prefix}:{host}"],
                args=[int(time.time() * 1000), interval, ttl_seconds],
            )
        except Exception:
            # Fail open on pacing, not closed. A Redis outage must not stall
            # every crawl in the fleet; under-pacing for the duration is the
            # lesser harm, and it is logged so it is not invisible.
            log.warning("host_limiter.redis_unavailable_pacing_locally", host=host)
            await asyncio.sleep(_jittered(interval) / 1000)
            return
        if wait_ms and wait_ms > 0:
            await asyncio.sleep(_jittered(float(wait_ms)) / 1000)

    def penalize(self, host: str) -> None:
        self._backoff.penalize(host)

    def reward(self, host: str) -> None:
        self._backoff.reward(host)
