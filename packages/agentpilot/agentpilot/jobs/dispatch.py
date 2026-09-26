"""Memory-aware concurrency for the crawl worker.

`CrawlWorkerLoop.tick()` admits `max_concurrent` tasks per batch regardless of
what the machine is doing. Each admitted task opens a browser context, and a
Chrome context on a heavy page is hundreds of megabytes, so the failure mode is
not gradual: the box runs out of memory and the OOM killer takes the worker
process, losing every in-flight task's lease along with it. The tasks come back
(`reclaim_stale_tasks` requeues them) but only after `stale_after_seconds`, and
the worker that killed itself is likely to do it again on the retry.

This reads how much memory is actually left and admits fewer tasks when the
answer is "not much".

**Why not psutil.** crawl4ai's `MemoryAdaptiveDispatcher` uses
`psutil.virtual_memory().percent` (Apache-2.0; `crawl4ai/async_dispatcher.py`),
which is right for a library running in somebody's Python process on their own
machine and wrong here: inside a container that reports the *host's* memory, not
the container's limit. A worker capped at 2 GB on a 64 GB host sees 8% usage
while it is being OOM-killed. The cgroup is the number that decides whether this
process lives, so the cgroup is what this reads -- which also means no new
dependency.

Returns `None` rather than guessing when it cannot tell (a macOS dev machine has
no cgroup filesystem), and `None` means "admit the full batch" -- throttling on a
number this module does not actually have would be worse than not throttling.
"""

from __future__ import annotations

from pathlib import Path

import structlog

log = structlog.get_logger(__name__)

_CGROUP_V2_CURRENT = Path("/sys/fs/cgroup/memory.current")
_CGROUP_V2_MAX = Path("/sys/fs/cgroup/memory.max")
_CGROUP_V1_USAGE = Path("/sys/fs/cgroup/memory/memory.usage_in_bytes")
_CGROUP_V1_LIMIT = Path("/sys/fs/cgroup/memory/memory.limit_in_bytes")
_PROC_MEMINFO = Path("/proc/meminfo")

# Above an unlimited cgroup's sentinel there is no limit to be a fraction of.
# cgroup v1 writes a huge number instead of v2's literal "max"; anything this
# large is that sentinel rather than a real limit.
_V1_UNLIMITED_THRESHOLD = 1 << 62

SOFT_PRESSURE = 0.70
"""Above this fraction of the memory limit, halve the batch. Chosen below the
point where things actually break: a browser context that has not been opened
yet is the cheapest one to give up, and backing off at 70% leaves room for the
contexts already running to grow into their peak."""

HARD_PRESSURE = 0.85
"""Above this, admit one task at a time. Not zero -- a worker that admits nothing
makes no progress and never drains the queue, so the job stalls silently instead
of finishing slowly. One at a time still finishes."""


def read_memory_pressure() -> float | None:
    """Fraction of the applicable memory limit currently in use, or `None` when
    this platform will not say.

    Order matters: the cgroup is checked before `/proc/meminfo` because in a
    container both exist and only the cgroup describes the limit this process
    will be killed for exceeding.
    """

    for reader in (_cgroup_v2, _cgroup_v1, _proc_meminfo):
        try:
            pressure = reader()
        except (OSError, ValueError):
            continue
        if pressure is not None:
            return pressure
    return None


def _cgroup_v2() -> float | None:
    if not _CGROUP_V2_CURRENT.exists() or not _CGROUP_V2_MAX.exists():
        return None
    limit_raw = _CGROUP_V2_MAX.read_text().strip()
    if limit_raw == "max":
        # No limit set on this cgroup, so there is no fraction to report --
        # fall through to /proc/meminfo, which at least describes the machine.
        return None
    limit = float(limit_raw)
    if limit <= 0:
        return None
    return float(_CGROUP_V2_CURRENT.read_text().strip()) / limit


def _cgroup_v1() -> float | None:
    if not _CGROUP_V1_USAGE.exists() or not _CGROUP_V1_LIMIT.exists():
        return None
    limit = float(_CGROUP_V1_LIMIT.read_text().strip())
    if limit <= 0 or limit >= _V1_UNLIMITED_THRESHOLD:
        return None
    return float(_CGROUP_V1_USAGE.read_text().strip()) / limit


def _proc_meminfo() -> float | None:
    """Uses `MemAvailable`, not `MemTotal - MemFree`.

    `MemFree` excludes page cache and reclaimable slab, so a healthy Linux box
    running anything at all reports almost no free memory and this would throttle
    permanently. `MemAvailable` is the kernel's own estimate of what a new
    allocation could actually get, which is the question being asked.
    """

    if not _PROC_MEMINFO.exists():
        return None
    total_kb: float | None = None
    available_kb: float | None = None
    for line in _PROC_MEMINFO.read_text().splitlines():
        if line.startswith("MemTotal:"):
            total_kb = float(line.split()[1])
        elif line.startswith("MemAvailable:"):
            available_kb = float(line.split()[1])
        if total_kb is not None and available_kb is not None:
            break
    if not total_kb or available_kb is None:
        return None
    return 1.0 - (available_kb / total_kb)


def permits(base: int, *, pressure: float | None = None) -> int:
    """How many tasks to admit this batch, given `base` as the configured
    ceiling. Never returns less than 1, and never more than `base`."""

    if pressure is None:
        pressure = read_memory_pressure()
    if pressure is None:
        return max(base, 1)

    if pressure >= HARD_PRESSURE:
        allowed = 1
    elif pressure >= SOFT_PRESSURE:
        allowed = max(base // 2, 1)
    else:
        allowed = base

    if allowed < base:
        log.info(
            "crawl_worker_loop.concurrency_reduced",
            memory_pressure=round(pressure, 3),
            configured=base,
            admitted=allowed,
        )
    return max(min(allowed, base), 1)
