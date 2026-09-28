"""Whether this node can afford to launch one more browser.

The reaper answers the question *after* the fact: it evicts contexts once the
node is already under pressure. That works only while there is something idle to
take, and under sustained load there never is -- every context is ACTIVE because
every one of them is being used. So the watermark sits there, correct and
powerless, while the node fills up.

MEASURED. Two recipe workers on a 7.65 GB host reached 133 Chrome processes
holding 6.7 GB, and a build died mid-run with `page_crash`. The reaper's
memory-pressure scan ran the whole time and evicted nothing, because nothing was
IDLE. Its per-context ceiling could not help either -- it was reading Chrome's
root process (~290 MB) rather than the tree.

**The load was never admission-controlled at all.** `AGENTPILOT_MAX_CONTEXTS_PER_NODE`
is enforced by the *gateway's* placer, and `recipe_worker_loop` opens its browser
in-process, deliberately: "A worker opens its browser in-process, which never
goes through the gateway's placer". So the one workload that filled the host was
the one nothing counted.

This is the missing half, and it is the shape firecrawl's playwright service
uses: a hard `Semaphore(MAX_CONCURRENT_PAGES)` acquired before a page is opened,
so the N+1th caller waits or is refused rather than piling on. The difference
here is what a "slot" costs -- firecrawl opens a *context* in one shared browser,
crawlpilot opens a whole browser per identity, because warm per-identity profiles
are the anti-detection feature (`ephemeral.py`: a cookie-less first-visit browser
is itself a bot signal). A slot is therefore worth hundreds of megabytes rather
than a few, and the budget has to be counted in both contexts and bytes.

Refusal is `CapacityExhausted`, which already carries `http_status = 503` and
`retry_after_seconds = 5`. A caller that waits and retries is exactly right: the
pressure is transient, and the alternative -- opening anyway -- is what produced
the crash.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import structlog

from crawlpilot.spi.errors import CapacityExhausted

log = structlog.get_logger(__name__)

#: Called immediately before a new browser is launched, and only then -- reusing
#: a warm context costs no new memory and must never be refused. Raises
#: `CapacityExhausted` to decline.
Admit = Callable[[], Awaitable[None]]


class NodeAdmission:
    """Counts what a node is already carrying, and says no before it is too late.

    Two independent budgets, because either alone is escapable:

    - **contexts**, which bounds the steady state. One browser per identity, and
      a node that is allowed 25 of them on hardware that fits four will reach
      four and then fail confusingly.
    - **memory**, which bounds the pathological case a count cannot see. Four
      contexts is a fine number right up until one of them opens a page that
      takes a gigabyte.

    `live_contexts` is injected rather than read from a registry directly so this
    stays testable without one, and so the caller decides what counts as live.
    """

    def __init__(
        self,
        *,
        live_contexts: Callable[[], Awaitable[int]],
        max_contexts: int,
        used_pct: Callable[[], float | None],
        watermark_pct: float,
    ) -> None:
        self._live_contexts = live_contexts
        self._max_contexts = max_contexts
        self._used_pct = used_pct
        self._watermark_pct = watermark_pct

    async def __call__(self) -> None:
        live = await self._live_contexts()
        if live >= self._max_contexts:
            log.warning(
                "admission.refused_max_contexts",
                live=live,
                max_contexts=self._max_contexts,
            )
            raise CapacityExhausted(
                f"this node already holds {live} browser contexts "
                f"(max {self._max_contexts}); retry shortly"
            )

        used = self._used_pct()
        if used is not None and used >= self._watermark_pct:
            log.warning(
                "admission.refused_memory", used_pct=round(used, 1),
                watermark_pct=self._watermark_pct, live=live,
            )
            raise CapacityExhausted(
                f"this node is at {used:.0f}% memory (watermark "
                f"{self._watermark_pct:.0f}%); retry shortly"
            )
