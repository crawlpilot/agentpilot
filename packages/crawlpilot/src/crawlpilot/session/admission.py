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

Refusal is `NodeAtCapacity`, which carries `http_status = 503` and
`retry_after_seconds = 5`. A caller that waits and retries is exactly right: the
pressure is transient, and the alternative -- opening anyway -- is what produced
the crash. That exception exists as its own class precisely so this can be true;
see its docstring for the run it cost when the refusal was classified permanent.

**Counting moved out of here, and that was the fix.** This used to take a
`live_contexts` callable and compare it to a ceiling, which had two faults that
`slots.py` documents in full: the count came from a keyspace scan over keys
nothing expired, and it was read in Python one step before the browser opened, so
two identities could pass the same check. What is left here is the half that
genuinely cannot live in a Lua script -- reading `/proc/meminfo`. The contended
half, the context count, moved into the registry backends where it can be
counted and claimed in the same atomic step.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import structlog

from crawlpilot.spi.errors import NodeAtCapacity

log = structlog.get_logger(__name__)

#: Called immediately before a new browser is launched, with the identity slug
#: that browser will belong to. Raises `NodeAtCapacity` to decline. A warm reuse
#: passes its own slug too -- the slot table treats that as a renewal and never
#: refuses it, because the browser already exists and refusing frees nothing.
Admit = Callable[[str], Awaitable[None]]


class NodeAdmission:
    """The memory half of admission -- the half that cannot be made atomic.

    Two budgets bound a node, and they need opposite treatment:

    - **contexts**, which bounds the steady state. One browser per identity, and
      a node allowed 25 of them on hardware that fits four will reach four and
      then fail confusingly. This is contended -- several callers race for the
      last slot -- so it has to be counted and claimed in one step, and it lives
      in the registry backend: `acquire_lease.lua` for Redis, the `SlotTable`
      for the in-memory one.
    - **memory**, which bounds the pathological case a count cannot see. Four
      contexts is a fine number right up until one of them opens a page that
      takes a gigabyte.

    Memory stays here because it is a property of the *node*, not of any claim:
    nobody races anybody for it, reading `/proc/meminfo` cannot happen inside a
    Lua script, and refusing on it costs nothing because no slot has been taken
    yet. Splitting the two this way is what let the contended half become atomic
    at all.
    """

    def __init__(
        self,
        *,
        used_pct: Callable[[], float | None],
        watermark_pct: float,
    ) -> None:
        self._used_pct = used_pct
        self._watermark_pct = watermark_pct

    async def __call__(self, slug: str) -> None:
        used = self._used_pct()
        if used is None or used < self._watermark_pct:
            return
        log.warning(
            "admission.refused_memory",
            slug=slug,
            used_pct=round(used, 1),
            watermark_pct=self._watermark_pct,
        )
        raise NodeAtCapacity(
            f"this node is at {used:.0f}% memory (watermark "
            f"{self._watermark_pct:.0f}%); retry shortly"
        )
