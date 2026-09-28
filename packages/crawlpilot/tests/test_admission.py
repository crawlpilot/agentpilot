"""Refusing a browser before the node is full, rather than evicting after.

The reaper is the other half of this policy and it can only take back what is
IDLE. Under sustained load nothing is idle, so the watermark sits there correct
and powerless -- measured at 133 Chrome processes holding 6.7 of 7.65 GB, with a
build dying mid-run while the memory-pressure scan evicted nothing.
"""

from __future__ import annotations

import pytest

from crawlpilot.session.admission import NodeAdmission
from crawlpilot.spi.errors import NodeAtCapacity
from crawlpilot.spi.lease import ContextRef


def admission(*, used: float | None = 10.0, watermark: float = 85.0) -> NodeAdmission:
    """`NodeAdmission` is the MEMORY half only.

    The context count moved into the registry backends -- `acquire_lease.lua`
    for Redis, `SlotTable` for the in-memory one -- because counting and
    claiming have to be one atomic step and neither could be, from here. See
    `test_slots.py` for the budget's own tests.
    """

    return NodeAdmission(used_pct=lambda: used, watermark_pct=watermark)


async def test_room_to_spare_admits() -> None:
    await admission()("t/example.com/a")


async def test_memory_refuses_even_with_contexts_to_spare() -> None:
    """The pathological case a count cannot see: four contexts is a fine number
    right up until one of them opens a page that takes a gigabyte."""

    with pytest.raises(NodeAtCapacity) as exc:
        await admission(used=91.0)("t/example.com/a")
    assert "91% memory" in str(exc.value)


async def test_an_unreadable_meminfo_does_not_refuse() -> None:
    """Off Linux `read_meminfo_used_pct` answers None. A gauge that cannot be
    read is not evidence of pressure, and refusing on it would make the whole
    node unusable everywhere but Linux."""

    await admission(used=None)("t/example.com/a")


async def test_exactly_at_the_watermark_refuses() -> None:
    """The watermark is the point at which the node is already in trouble, not
    a target to reach."""

    with pytest.raises(NodeAtCapacity):
        await admission(used=85.0, watermark=85.0)("t/example.com/a")


# --- the registry only asks when it is about to LAUNCH ----------------------


def _identity(name: str = "n"):
    from crawlpilot.spi.identity import IdentityRef

    return IdentityRef(key=f"t/example.com/{name}")


def _opener(identity, counter: list[int]):
    async def opener():
        from crawlpilot.spi.lease import ContextRef, ContextState

        counter[0] += 1
        return ContextRef(
            context_id=f"ctx-{counter[0]}",
            identity=identity,
            state=ContextState.ACTIVE,
            pid=1000 + counter[0],
        )

    return opener


async def test_reuse_of_a_warm_context_is_never_refused() -> None:
    """The property that makes the gate safe to have at all.

    Reusing a warm browser costs no new memory, so refusing it is pure loss --
    and worse, a node at its ceiling would become unable to run the very work
    that would let it release a context.
    """

    from crawlpilot.session.registry import Registry

    asked = [0]

    async def admit(slug: str) -> None:
        asked[0] += 1

    identity = _identity()
    opened = [0]
    registry = Registry(admit=admit)

    _ctx, lease = await registry.acquire(identity, "owner", 60.0, _opener(identity, opened))
    assert (asked[0], opened[0]) == (1, 1)

    await registry.release(lease.lease_id)

    # Second acquire reuses the warm entry: no launch, so no question asked.
    await registry.acquire(identity, "owner", 60.0, _opener(identity, opened))
    assert (asked[0], opened[0]) == (1, 1), "a warm reuse must not consult admission"


async def test_a_refusal_does_not_launch_anything() -> None:
    from crawlpilot.session.registry import Registry

    async def admit(slug: str) -> None:
        raise NodeAtCapacity("full")

    opened = [0]
    identity = _identity()
    registry = Registry(admit=admit)

    with pytest.raises(NodeAtCapacity):
        await registry.acquire(identity, "owner", 60.0, _opener(identity, opened))
    assert opened[0] == 0


async def test_no_admit_hook_behaves_exactly_as_before() -> None:
    """Every existing caller constructs `Registry()` with no hook, and must be
    unaffected."""

    from crawlpilot.session.registry import Registry

    opened = [0]
    identity = _identity()
    registry = Registry()
    await registry.acquire(identity, "owner", 60.0, _opener(identity, opened))
    assert opened[0] == 1


# --- the budget, as the registry actually applies it -------------------------


async def test_the_registry_refuses_a_fifth_browser_and_a_destroy_makes_room() -> None:
    """End to end through `Registry`, not just the slot table.

    A `release` must NOT make room -- it moves the context to IDLE and Chrome is
    still running and still resident. Only `evict`, which is what the reaper
    calls before `driver.close()`, gives the memory back. Freeing the slot at
    release would let a node admit a fifth browser while four were live, which
    is the same over-admission the budget exists to prevent, reached from the
    other side.
    """

    from crawlpilot.session.registry import Registry
    from crawlpilot.session.slots import InMemorySlotTable
    from crawlpilot.spi.identity import IdentityRef

    registry = Registry(slots=InMemorySlotTable(), max_contexts=2, slot_ttl_seconds=60)
    opened = [0]
    leases = {}

    for i in range(2):
        identity = IdentityRef(key=f"t/example.com/s{i}")
        _ctx, lease = await registry.acquire(
            identity, "owner", 60.0, _opener(identity, opened)
        )
        leases[identity] = lease
    assert opened[0] == 2

    third = IdentityRef(key="t/example.com/s2")
    with pytest.raises(NodeAtCapacity):
        await registry.acquire(third, "owner", 60.0, _opener(third, opened))
    assert opened[0] == 2, "a refusal must not launch a browser"

    # IDLE is still resident: the node is carrying it, so the budget still says so.
    first = IdentityRef(key="t/example.com/s0")
    await registry.release(leases[first].lease_id)
    with pytest.raises(NodeAtCapacity):
        await registry.acquire(third, "owner", 60.0, _opener(third, opened))

    # Destroying it is what frees the slot.
    await registry.evict(first)
    await registry.acquire(third, "owner", 60.0, _opener(third, opened))
    assert opened[0] == 3


async def test_a_failed_open_gives_its_slot_back() -> None:
    """The slot is claimed a line before the browser launches, so a launch that
    raises would otherwise bill the node for a context that does not exist --
    right only once the deadline passed, and the deadline is sized as a crash
    backstop, not a retry interval."""

    from crawlpilot.session.registry import Registry
    from crawlpilot.session.slots import InMemorySlotTable
    from crawlpilot.spi.identity import IdentityRef

    slots = InMemorySlotTable()
    registry = Registry(slots=slots, max_contexts=1, slot_ttl_seconds=60)

    async def _explode() -> ContextRef:
        raise RuntimeError("chrome would not start")

    with pytest.raises(RuntimeError):
        await registry.acquire(IdentityRef(key="t/example.com/a"), "owner", 60.0, _explode)

    assert await slots.live() == 0
    opened = [0]
    other = IdentityRef(key="t/example.com/b")
    await registry.acquire(other, "owner", 60.0, _opener(other, opened))
    assert opened[0] == 1
