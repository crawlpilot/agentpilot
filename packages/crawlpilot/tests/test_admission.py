"""Refusing a browser before the node is full, rather than evicting after.

The reaper is the other half of this policy and it can only take back what is
IDLE. Under sustained load nothing is idle, so the watermark sits there correct
and powerless -- measured at 133 Chrome processes holding 6.7 of 7.65 GB, with a
build dying mid-run while the memory-pressure scan evicted nothing.
"""

from __future__ import annotations

import pytest

from crawlpilot.session.admission import NodeAdmission
from crawlpilot.spi.errors import CapacityExhausted


def admission(*, live: int, max_contexts: int = 4, used: float | None = 10.0,
              watermark: float = 85.0) -> NodeAdmission:
    async def _live() -> int:
        return live

    return NodeAdmission(
        live_contexts=_live,
        max_contexts=max_contexts,
        used_pct=lambda: used,
        watermark_pct=watermark,
    )


async def test_room_to_spare_admits() -> None:
    await admission(live=1)()


async def test_the_context_budget_refuses_before_the_node_is_full() -> None:
    with pytest.raises(CapacityExhausted) as exc:
        await admission(live=4, max_contexts=4)()
    assert "4 browser contexts" in str(exc.value)


async def test_a_full_node_refuses_rather_than_queueing_forever() -> None:
    """`CapacityExhausted` already carries 503 + Retry-After 5, so a caller that
    waits and retries is exactly right: the pressure is transient, and opening
    anyway is what produced the crash."""

    assert CapacityExhausted.http_status == 503
    assert CapacityExhausted.retry_after_seconds == 5


async def test_memory_refuses_even_with_contexts_to_spare() -> None:
    """The pathological case a count cannot see: four contexts is a fine number
    right up until one of them opens a page that takes a gigabyte."""

    with pytest.raises(CapacityExhausted) as exc:
        await admission(live=1, used=91.0)()
    assert "91% memory" in str(exc.value)


async def test_an_unreadable_meminfo_does_not_refuse() -> None:
    """Off Linux `read_meminfo_used_pct` answers None. A gauge that cannot be
    read is not evidence of pressure, and refusing on it would make the whole
    node unusable everywhere but Linux."""

    await admission(live=1, used=None)()


async def test_exactly_at_the_watermark_refuses() -> None:
    """The watermark is the point at which the node is already in trouble, not
    a target to reach."""

    with pytest.raises(CapacityExhausted):
        await admission(live=1, used=85.0, watermark=85.0)()


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

    async def admit() -> None:
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

    async def admit() -> None:
        raise CapacityExhausted("full")

    opened = [0]
    identity = _identity()
    registry = Registry(admit=admit)

    with pytest.raises(CapacityExhausted):
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
