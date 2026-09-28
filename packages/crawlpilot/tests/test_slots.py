"""Unit tests for `crawlpilot.session.slots` -- the expiry-as-score budget.

No Redis and no browser: the in-memory table implements the same semantics the
Lua script does, so the rules can be pinned here and the script only has to
agree with them.
"""

from __future__ import annotations

import asyncio

import pytest

from crawlpilot.session.slots import InMemorySlotTable
from crawlpilot.spi.errors import CapacityExhausted, NodeAtCapacity


async def test_claims_up_to_the_limit_then_refuses() -> None:
    slots = InMemorySlotTable()
    for i in range(4):
        await slots.claim(f"t/example.com/s{i}", ttl_seconds=60, max_slots=4)

    assert await slots.live() == 4
    with pytest.raises(NodeAtCapacity):
        await slots.claim("t/example.com/s4", ttl_seconds=60, max_slots=4)


async def test_a_holder_that_never_renews_stops_counting_on_its_own() -> None:
    """The property the whole design exists for.

    Nothing runs here -- no reaper, no sweep called by the test, no cleanup
    path. The slot stops counting because its deadline passed, which is what
    makes a crashed worker harmless instead of permanently expensive. The old
    count came from scanning `active:*` keys that no script ever expired, so a
    dead holder was counted forever and a node stayed "full" after one unclean
    restart.
    """

    slots = InMemorySlotTable()
    await slots.claim("t/example.com/dead", ttl_seconds=0.01, max_slots=1)
    assert await slots.live() == 1

    await asyncio.sleep(0.02)

    assert await slots.live() == 0
    await slots.claim("t/example.com/fresh", ttl_seconds=60, max_slots=1)


async def test_an_existing_holder_is_never_refused_at_the_limit() -> None:
    """A warm reuse costs no new memory, so refusing it frees nothing and
    stops the node doing the work that would free a slot. `acquire_lease.lua`
    spells the same rule as `reuse == 0 and ZSCORE == false`."""

    slots = InMemorySlotTable()
    await slots.claim("t/example.com/a", ttl_seconds=60, max_slots=1)

    # Full, and yet the holder may claim again -- that is a renewal.
    await slots.claim("t/example.com/a", ttl_seconds=60, max_slots=1)
    assert await slots.live() == 1


async def test_a_refusal_claims_nothing() -> None:
    slots = InMemorySlotTable()
    await slots.claim("t/example.com/a", ttl_seconds=60, max_slots=1)

    with pytest.raises(NodeAtCapacity):
        await slots.claim("t/example.com/b", ttl_seconds=60, max_slots=1)

    # The refused slug must not have been recorded; releasing the holder has to
    # be enough to make room.
    await slots.release("t/example.com/a")
    assert await slots.live() == 0


async def test_renew_pushes_the_deadline_out_without_consulting_the_budget() -> None:
    slots = InMemorySlotTable()
    await slots.claim("t/example.com/a", ttl_seconds=0.01, max_slots=1)
    await slots.renew("t/example.com/a", ttl_seconds=60)

    await asyncio.sleep(0.02)

    assert await slots.live() == 1


async def test_concurrent_claims_for_different_slugs_cannot_both_win() -> None:
    """The regression. The previous gate read a count in Python and opened the
    browser after, and the registry's lock is per-IDENTITY -- so two *different*
    identities serialized against nothing and both passed the same check. The
    log recorded `admission.refused_max_contexts live=5 max_contexts=4`: a limit
    of four, breached, by the thing enforcing it.
    """

    slots = InMemorySlotTable()
    results = await asyncio.gather(
        *(slots.claim(f"t/example.com/s{i}", ttl_seconds=60, max_slots=1) for i in range(8)),
        return_exceptions=True,
    )

    won = [r for r in results if not isinstance(r, BaseException)]
    refused = [r for r in results if isinstance(r, NodeAtCapacity)]
    assert len(won) == 1
    assert len(refused) == 7
    assert await slots.live() == 1


def test_refusal_is_a_capacity_error_on_the_wire() -> None:
    """`NodeAtCapacity` exists to be classified differently *in process* while
    staying identical to every client. Subclassing is what buys both."""

    assert issubclass(NodeAtCapacity, CapacityExhausted)
    assert NodeAtCapacity.http_status == 503
    assert NodeAtCapacity.code == CapacityExhausted.code
    assert NodeAtCapacity.retry_after_seconds == 5
